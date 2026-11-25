// PAPro community packs: a small Cloudflare Worker (R2 storage).
//   POST /submit            multipart: pack (zip), meta (json), author_key        -> pending review
//   GET  /index.json        approved packs, same format as catalog.json
//   GET  /packs/<id>.zip    an approved pack
//   POST /rate              json {id, stars 1-5, rater_key}                        -> one vote per install, can be changed
//   POST /claim             json {code, author_key}                               -> a pack is handed to a new PAPro install
//   POST /withdraw          json {id, author_key}                                 -> the author removes their own pack
//   GET  /admin             your review page (Authorization: Bearer ADMIN_TOKEN, or ?token=)
//   POST /admin/approve|reject|remove/<id>
// Bindings: PACKS (R2 bucket), ADMIN_TOKEN (secret), AUTO_APPROVE ("1" to skip review), PUBLIC_URL (this worker's address).

const MAX_BYTES = 90 * 1024 * 1024;
const ALLOWED_TOP = new Set(["main", "dep", "dest", "fuel", "time", "extra", "voices", "airports"]);
const ALLOWED_FILES = new Set(["voice.json", "logo.png", "scripts.json", "languages.json"]);
const json = (o, s = 200) => new Response(JSON.stringify(o), { status: s, headers: { "content-type": "application/json", "access-control-allow-origin": "*" } });
const hex = buf => [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, "0")).join("");
const sha256 = async data => hex(await crypto.subtle.digest("SHA-256", data));
const slug = s => String(s || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 48);
const clean = (s, n) => String(s || "").replace(/[\u0000-\u001f<>]/g, "").trim().slice(0, n);

// the zip's file names, read from its central directory (nothing is unpacked here)
function zipNames(buf) {
  const v = new DataView(buf), n = buf.byteLength;
  let end = -1;
  for (let i = n - 22; i >= Math.max(0, n - 65557); i--) if (v.getUint32(i, true) === 0x06054b50) { end = i; break; }
  if (end < 0) throw new Error("not a zip file");
  const count = v.getUint16(end + 10, true);
  let p = v.getUint32(end + 16, true);
  const names = [], dec = new TextDecoder();
  for (let i = 0; i < count; i++) {
    if (v.getUint32(p, true) !== 0x02014b50) throw new Error("damaged zip");
    const nl = v.getUint16(p + 28, true), xl = v.getUint16(p + 30, true), cl = v.getUint16(p + 32, true);
    names.push(dec.decode(new Uint8Array(buf, p + 46, nl)));
    p += 46 + nl + xl + cl;
  }
  return names;
}

// the pack's logo.png, if it has one (stored or deflated), so the Community list can show it
async function zipLogo(buf) {
  const v = new DataView(buf), n = buf.byteLength;
  let end = -1;
  for (let i = n - 22; i >= Math.max(0, n - 65557); i--) if (v.getUint32(i, true) === 0x06054b50) { end = i; break; }
  if (end < 0) return null;
  const count = v.getUint16(end + 10, true);
  let p = v.getUint32(end + 16, true);
  for (let i = 0; i < count; i++) {
    if (v.getUint32(p, true) !== 0x02014b50) return null;
    const method = v.getUint16(p + 10, true), csize = v.getUint32(p + 20, true), usize = v.getUint32(p + 24, true);
    const nl = v.getUint16(p + 28, true), xl = v.getUint16(p + 30, true), cl = v.getUint16(p + 32, true), off = v.getUint32(p + 42, true);
    const name = new TextDecoder().decode(new Uint8Array(buf, p + 46, nl));
    if (name === "logo.png" && usize > 0 && usize <= 3 * 1024 * 1024) {
      const start = off + 30 + v.getUint16(off + 26, true) + v.getUint16(off + 28, true);
      const raw = new Uint8Array(buf, start, csize);
      if (method === 0) return raw.slice().buffer;
      if (method === 8) return await new Response(new Blob([raw]).stream().pipeThrough(new DecompressionStream("deflate-raw"))).arrayBuffer();
      return null;
    }
    p += 46 + nl + xl + cl;
  }
  return null;
}

function checkNames(names) {
  let oggs = 0, hasMain = false;
  for (const raw of names) {
    const name = raw.replace(/\\/g, "/");
    if (name.endsWith("/")) continue;
    const parts = name.split("/").filter(Boolean);
    if (!parts.length || parts.some(x => x === ".." || x.includes(":"))) throw new Error("unsafe path: " + raw);
    if (parts.length === 1 && ALLOWED_FILES.has(parts[0])) continue;
    if (!ALLOWED_TOP.has(parts[0])) throw new Error("unexpected file: " + raw);
    if (!/\.(ogg|json|png)$/i.test(parts[parts.length - 1])) throw new Error("only audio and pack files are allowed: " + raw);
    if (/\.ogg$/i.test(name)) oggs++;
    if (parts[0] === "main" || (parts[0] === "voices" && parts[2] === "main")) hasMain = true;
  }
  if (!hasMain || !oggs) throw new Error("the pack has no main folder with announcements");
  return oggs;
}

async function readIndex(env) {
  const o = await env.PACKS.get("index.json");
  return o ? await o.json() : { packs: [] };
}
const writeIndex = (env, idx) => env.PACKS.put("index.json", JSON.stringify(idx, null, 1));

// an email to you when a pack arrives (Resend, resend.com): needs RESEND_API_KEY and NOTIFY_EMAIL, otherwise nothing is sent
async function notify(env, rec) {
  if (!env.RESEND_API_KEY || !env.NOTIFY_EMAIL) return;
  const esc = s => String(s || "").replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  try {
    await fetch("https://api.resend.com/emails", {
      method: "POST",
      headers: { authorization: `Bearer ${env.RESEND_API_KEY}`, "content-type": "application/json" },
      body: JSON.stringify({
        from: env.NOTIFY_FROM || "PAPro <onboarding@resend.dev>", to: [env.NOTIFY_EMAIL],
        subject: `${rec.updated ? "Updated" : "New"} PAPro community pack: ${rec.name} (${rec.icao})`,
        html: `<p><b>${esc(rec.name)}</b> (${esc(rec.icao)}) - voice ${esc(rec.voice)}, v${esc(rec.version)}<br>By ${esc(rec.author)}<br>${rec.files} recordings, ${(rec.size / 1048576).toFixed(1)} MB</p>` +
          (rec.note ? `<p>"${esc(rec.note)}"</p>` : "") + `<p><a href="${env.PUBLIC_URL}/admin">Open the review page</a> (add ?token=your password)</p>`,
      }),
    });
  } catch { /* the pack is saved either way */ }
}

async function submit(req, env, ctx) {
  const form = await req.formData();
  const file = form.get("pack"), authorKey = clean(form.get("author_key"), 100);
  let meta;
  try { meta = JSON.parse(String(form.get("meta") || "{}")); } catch { return json({ error: "bad details" }, 400); }
  if (!file || typeof file === "string" || file.size > MAX_BYTES) return json({ error: "the pack is missing or too large (limit 90 MB)" }, 400);
  if (authorKey.length < 16) return json({ error: "missing author key" }, 400);
  if (meta.rights !== true) return json({ error: "the rights confirmation is required" }, 400);
  const icao = clean(meta.icao, 3).toUpperCase();
  if (!/^[A-Z0-9]{3}$/.test(icao)) return json({ error: "airline code must be 3 letters or digits" }, 400);
  const buf = await file.arrayBuffer();
  let oggs;
  try { oggs = checkNames(zipNames(buf)); } catch (e) { return json({ error: String(e.message || e) }, 400); }
  const id = slug(`${icao}-${meta.voice || meta.author || "pack"}`);
  const prior = await env.PACKS.get(`meta/${id}.json`);
  const authorHash = await sha256(new TextEncoder().encode(authorKey));
  const before = prior ? await prior.json() : null;
  if (before && before.author_hash !== authorHash) return json({ error: "a pack with this name already exists" }, 409);
  const update = !!before && before.status === "approved";   // the author's own, already approved pack: an update goes live without review
  const record = {
    id, icao, name: clean(meta.name, 60) || icao, voice: clean(meta.voice, 40), version: clean(meta.version, 20) || "1.0.0",
    author: clean(meta.author, 60), license: "", note: clean(meta.note, 300), files: oggs, size: buf.byteLength,
    sha256: await sha256(buf), author_hash: authorHash, submitted: new Date().toISOString(), status: "pending", updated: update,
  };
  const sample = form.get("sample");   // a short clip of the After Landing PA, cut on the author's PC
  if (sample && typeof sample !== "string" && sample.size > 0 && sample.size <= 400 * 1024) {
    const head = new Uint8Array(await sample.slice(0, 4).arrayBuffer());
    if (String.fromCharCode(...head) === "OggS") await env.PACKS.put(`samples/${id}.ogg`, await sample.arrayBuffer());
  }
  await env.PACKS.put(`pending/${id}.zip`, buf);
  await env.PACKS.put(`meta/${id}.json`, JSON.stringify(record));
  if (env.AUTO_APPROVE === "1" || update) await approve(env, id);
  const mail = notify(env, record);
  if (ctx && ctx.waitUntil) ctx.waitUntil(mail); else await mail;
  return json({ ok: true, id, status: env.AUTO_APPROVE === "1" || update ? "approved" : "pending", updated: update });
}

async function approve(env, id) {
  const m = await env.PACKS.get(`meta/${id}.json`), z = await env.PACKS.get(`pending/${id}.zip`) || await env.PACKS.get(`packs/${id}.zip`);
  if (!m || !z) throw new Error("not found");
  const rec = await m.json();
  const zbuf = await z.arrayBuffer();
  await env.PACKS.put(`packs/${id}.zip`, zbuf);
  let logo = null;
  try { logo = await zipLogo(zbuf); } catch { /* no logo is fine */ }
  if (logo) await env.PACKS.put(`logos/${id}.png`, logo); else await env.PACKS.delete(`logos/${id}.png`);
  const hasSample = !!(await env.PACKS.get(`samples/${id}.ogg`));
  await env.PACKS.delete(`pending/${id}.zip`);
  rec.status = "approved";
  await env.PACKS.put(`meta/${id}.json`, JSON.stringify(rec));
  const idx = await readIndex(env);
  idx.packs = idx.packs.filter(p => p.id !== id);
  idx.packs.push({
    id, icao: rec.icao, name: rec.name, voice: rec.voice, version: rec.version, author: rec.author, license: rec.license,
    sha256: rec.sha256, size: rec.size, url: `${env.PUBLIC_URL}/packs/${id}.zip`, ...(logo ? { logo: `${env.PUBLIC_URL}/logos/${id}.png` } : {}),
    ...(hasSample ? { sample: `${env.PUBLIC_URL}/samples/${id}.ogg` } : {}), ...(await ratingOf(env, id)),
  });
  await writeIndex(env, idx);
}

async function ratingOf(env, id) {
  const o = await env.PACKS.get(`ratings/${id}.json`);
  const votes = o ? Object.values(await o.json()) : [];
  return { votes: votes.length, rating: votes.length ? Math.round(votes.reduce((a, b) => a + b, 0) / votes.length * 10) / 10 : 0 };
}

async function rate(req, env) {
  const b = await req.json(), id = slug(b.id), stars = Math.round(Number(b.stars));
  const key = clean(b.rater_key, 100);
  if (!(stars >= 1 && stars <= 5) || key.length < 16) return json({ error: "bad rating" }, 400);
  const idx = await readIndex(env);
  const entry = idx.packs.find(p => p.id === id);
  if (!entry) return json({ error: "pack not found" }, 404);
  const file = `ratings/${id}.json`, o = await env.PACKS.get(file);
  const votes = o ? await o.json() : {};
  votes[(await sha256(new TextEncoder().encode(key))).slice(0, 24)] = stars;   // one vote per PAPro install, changeable
  await env.PACKS.put(file, JSON.stringify(votes));
  Object.assign(entry, await ratingOf(env, id));
  await writeIndex(env, idx);
  return json({ ok: true, rating: entry.rating, votes: entry.votes });
}

// You hand a pack over to its author's new PC: a one-time code (good for 7 days) that the author enters in PAPro
async function transfer(env, id) {
  if (!await env.PACKS.get(`meta/${id}.json`)) throw new Error("not found");
  const bytes = crypto.getRandomValues(new Uint8Array(6));
  const code = [...bytes].map(b => "ABCDEFGHJKMNPQRSTUVWXYZ23456789"[b % 31]).join("");
  await env.PACKS.put(`transfer/${code}.json`, JSON.stringify({ id, expires: Date.now() + 7 * 864e5 }));
  return code;
}

async function claim(req, env) {
  const b = await req.json(), code = String(b.code || "").toUpperCase().replace(/[^A-Z0-9]/g, ""), key = clean(b.author_key, 100);
  if (code.length !== 6 || key.length < 16) return json({ error: "bad code" }, 400);
  const t = await env.PACKS.get(`transfer/${code}.json`);
  if (!t) return json({ error: "That code is not valid" }, 404);
  const { id, expires } = await t.json();
  await env.PACKS.delete(`transfer/${code}.json`);
  if (Date.now() > expires) return json({ error: "That code has expired" }, 410);
  const m = await env.PACKS.get(`meta/${id}.json`);
  if (!m) return json({ error: "That pack no longer exists" }, 404);
  const rec = await m.json();
  rec.author_hash = await sha256(new TextEncoder().encode(key));
  await env.PACKS.put(`meta/${id}.json`, JSON.stringify(rec));
  return json({ ok: true, name: rec.name, voice: rec.voice });
}

async function remove(env, id, reject) {
  await env.PACKS.delete(`pending/${id}.zip`);
  if (!reject) await env.PACKS.delete(`packs/${id}.zip`);
  await env.PACKS.delete(`meta/${id}.json`);
  await env.PACKS.delete(`ratings/${id}.json`);
  await env.PACKS.delete(`logos/${id}.png`);
  await env.PACKS.delete(`samples/${id}.ogg`);
  const idx = await readIndex(env);
  idx.packs = idx.packs.filter(p => p.id !== id);
  await writeIndex(env, idx);
}

async function adminPage(env) {
  const list = await env.PACKS.list({ prefix: "meta/" });
  const rows = [];
  for (const o of list.objects) { const r = await (await env.PACKS.get(o.key)).json(); rows.push(r); }
  rows.sort((a, b) => (a.status === b.status ? 0 : a.status === "pending" ? -1 : 1) || b.submitted.localeCompare(a.submitted));
  const esc = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const body = rows.map(r => `<tr><td><b>${esc(r.name)}</b> (${esc(r.icao)})<br><small>${esc(r.voice)} · v${esc(r.version)} · by ${esc(r.author)}</small><br><small>${esc(r.note)}</small></td>
    <td>${r.files} files<br>${(r.size / 1048576).toFixed(1)} MB</td><td>${r.status}</td>
    <td>${r.status === "pending" ? `<button onclick="act('approve','${r.id}')">Approve</button> <button onclick="act('reject','${r.id}')">Reject</button>` : `<button onclick="act('approve','${r.id}')">Refresh</button> <button onclick="act('transfer','${r.id}')">Transfer</button> <button onclick="act('remove','${r.id}')">Remove</button>`}
    <br><a href="/admin/download/${r.id}">Download zip</a></td></tr>`).join("");
  return new Response(`<!doctype html><meta name=viewport content="width=device-width"><title>PAPro community</title>
<style>body{font:15px system-ui;margin:24px;max-width:900px}td{padding:10px;border-bottom:1px solid #8884;vertical-align:top}button{padding:6px 12px}</style>
<h1>Community packs</h1><table>${body || "<tr><td>Nothing submitted yet.</td></tr>"}</table>
<script>const T=new URLSearchParams(location.search).get("token")||"";
async function act(a,id){const r=await fetch("/admin/"+a+"/"+id,{method:"POST",headers:{Authorization:"Bearer "+T}});
if(a==="transfer"){const j=await r.json();prompt("Give the author this code (works once, for 7 days):",j.code);return}location.reload()}</script>`, { headers: { "content-type": "text/html; charset=utf-8" } });
}

export default {
  async fetch(req, env, ctx) {
    const url = new URL(req.url), path = url.pathname;
    if (req.method === "OPTIONS") return new Response(null, { headers: { "access-control-allow-origin": "*", "access-control-allow-headers": "*" } });
    try {
      if (path === "/index.json") return json(await readIndex(env));
      let m = path.match(/^\/packs\/([a-z0-9-]+)\.zip$/);
      if (m) { const o = await env.PACKS.get(`packs/${m[1]}.zip`); return o ? new Response(o.body, { headers: { "content-type": "application/zip" } }) : json({ error: "not found" }, 404); }
      m = path.match(/^\/logos\/([a-z0-9-]+)\.png$/);
      if (m) { const o = await env.PACKS.get(`logos/${m[1]}.png`); return o ? new Response(o.body, { headers: { "content-type": "image/png", "cache-control": "public, max-age=3600" } }) : json({ error: "not found" }, 404); }
      m = path.match(/^\/samples\/([a-z0-9-]+)\.ogg$/);
      if (m) { const o = await env.PACKS.get(`samples/${m[1]}.ogg`); return o ? new Response(o.body, { headers: { "content-type": "audio/ogg", "cache-control": "no-cache", "access-control-allow-origin": "*" } }) : json({ error: "not found" }, 404); }
      if (path === "/submit" && req.method === "POST") return await submit(req, env, ctx);
      if (path === "/rate" && req.method === "POST") return await rate(req, env);
      if (path === "/claim" && req.method === "POST") return await claim(req, env);
      if (path === "/withdraw" && req.method === "POST") {
        const b = await req.json(), o = await env.PACKS.get(`meta/${slug(b.id)}.json`);
        if (!o) return json({ error: "not found" }, 404);
        if ((await o.json()).author_hash !== await sha256(new TextEncoder().encode(String(b.author_key || "")))) return json({ error: "not your pack" }, 403);
        await remove(env, slug(b.id), false);
        return json({ ok: true });
      }
      if (path.startsWith("/admin")) {
        const token = (req.headers.get("authorization") || "").replace(/^Bearer /, "") || url.searchParams.get("token") || "";
        if (!env.ADMIN_TOKEN || token !== env.ADMIN_TOKEN) return new Response("Not allowed", { status: 401 });
        if (path === "/admin") return await adminPage(env);
        m = path.match(/^\/admin\/(approve|reject|remove|download|transfer)\/([a-z0-9-]+)$/);
        if (m && m[1] === "download") { const o = await env.PACKS.get(`pending/${m[2]}.zip`) || await env.PACKS.get(`packs/${m[2]}.zip`); return o ? new Response(o.body, { headers: { "content-type": "application/zip" } }) : json({ error: "not found" }, 404); }
        if (m && m[1] === "transfer" && req.method === "POST") return json({ ok: true, code: await transfer(env, m[2]) });
        if (m && req.method === "POST") { if (m[1] === "approve") await approve(env, m[2]); else await remove(env, m[2], m[1] === "reject"); return json({ ok: true }); }
      }
      return json({ error: "not found" }, 404);
    } catch (e) { return json({ error: String(e.message || e) }, 500); }
  },
};
