// PAPro community packs: a small Cloudflare Worker (R2 storage).
//   POST /submit            multipart: pack (zip), meta (json), author_key        -> pending review
//   GET  /index.json        approved packs, same format as catalog.json
//   GET  /packs/<id>.zip    an approved pack
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

async function submit(req, env) {
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
  if (prior && (await prior.json()).author_hash !== authorHash) return json({ error: "a pack with this name already exists" }, 409);
  const record = {
    id, icao, name: clean(meta.name, 60) || icao, voice: clean(meta.voice, 40), version: clean(meta.version, 20) || "1.0.0",
    author: clean(meta.author, 60), license: "", note: clean(meta.note, 300), files: oggs, size: buf.byteLength,
    sha256: await sha256(buf), author_hash: authorHash, submitted: new Date().toISOString(), status: "pending",
  };
  await env.PACKS.put(`pending/${id}.zip`, buf);
  await env.PACKS.put(`meta/${id}.json`, JSON.stringify(record));
  if (env.AUTO_APPROVE === "1") await approve(env, id);
  return json({ ok: true, id, status: env.AUTO_APPROVE === "1" ? "approved" : "pending" });
}

async function approve(env, id) {
  const m = await env.PACKS.get(`meta/${id}.json`), z = await env.PACKS.get(`pending/${id}.zip`) || await env.PACKS.get(`packs/${id}.zip`);
  if (!m || !z) throw new Error("not found");
  const rec = await m.json();
  await env.PACKS.put(`packs/${id}.zip`, await z.arrayBuffer());
  await env.PACKS.delete(`pending/${id}.zip`);
  rec.status = "approved";
  await env.PACKS.put(`meta/${id}.json`, JSON.stringify(rec));
  const idx = await readIndex(env);
  idx.packs = idx.packs.filter(p => p.id !== id);
  idx.packs.push({
    id, icao: rec.icao, name: rec.name, voice: rec.voice, version: rec.version, author: rec.author, license: rec.license,
    sha256: rec.sha256, size: rec.size, url: `${env.PUBLIC_URL}/packs/${id}.zip`,
  });
  await writeIndex(env, idx);
}

async function remove(env, id, reject) {
  await env.PACKS.delete(`pending/${id}.zip`);
  if (!reject) await env.PACKS.delete(`packs/${id}.zip`);
  await env.PACKS.delete(`meta/${id}.json`);
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
    <td>${r.status === "pending" ? `<button onclick="act('approve','${r.id}')">Approve</button> <button onclick="act('reject','${r.id}')">Reject</button>` : `<button onclick="act('remove','${r.id}')">Remove</button>`}
    <br><a href="/admin/download/${r.id}">Download zip</a></td></tr>`).join("");
  return new Response(`<!doctype html><meta name=viewport content="width=device-width"><title>PAPro community</title>
<style>body{font:15px system-ui;margin:24px;max-width:900px}td{padding:10px;border-bottom:1px solid #8884;vertical-align:top}button{padding:6px 12px}</style>
<h1>Community packs</h1><table>${body || "<tr><td>Nothing submitted yet.</td></tr>"}</table>
<script>const T=new URLSearchParams(location.search).get("token")||"";
async function act(a,id){await fetch("/admin/"+a+"/"+id,{method:"POST",headers:{Authorization:"Bearer "+T}});location.reload()}</script>`, { headers: { "content-type": "text/html; charset=utf-8" } });
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url), path = url.pathname;
    if (req.method === "OPTIONS") return new Response(null, { headers: { "access-control-allow-origin": "*", "access-control-allow-headers": "*" } });
    try {
      if (path === "/index.json") return json(await readIndex(env));
      let m = path.match(/^\/packs\/([a-z0-9-]+)\.zip$/);
      if (m) { const o = await env.PACKS.get(`packs/${m[1]}.zip`); return o ? new Response(o.body, { headers: { "content-type": "application/zip" } }) : json({ error: "not found" }, 404); }
      if (path === "/submit" && req.method === "POST") return await submit(req, env);
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
        m = path.match(/^\/admin\/(approve|reject|remove|download)\/([a-z0-9-]+)$/);
        if (m && m[1] === "download") { const o = await env.PACKS.get(`pending/${m[2]}.zip`) || await env.PACKS.get(`packs/${m[2]}.zip`); return o ? new Response(o.body, { headers: { "content-type": "application/zip" } }) : json({ error: "not found" }, 404); }
        if (m && req.method === "POST") { if (m[1] === "approve") await approve(env, m[2]); else await remove(env, m[2], m[1] === "reject"); return json({ ok: true }); }
      }
      return json({ error: "not found" }, 404);
    } catch (e) { return json({ error: String(e.message || e) }, 500); }
  },
};
