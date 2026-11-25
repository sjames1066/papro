# PAPro community packs (service)

A small Cloudflare Worker with an R2 bucket. Free tier is enough to start.

## Set up (once)
1. `npm i -g wrangler` and `wrangler login`
2. `wrangler r2 bucket create papro-community`
3. Edit `wrangler.toml`: set `PUBLIC_URL` to your worker's address.
4. `wrangler secret put ADMIN_TOKEN` (any long random text: this is your review password)
5. `wrangler deploy`
6. Put the worker's address + `/index.json` into PAPro's `community_url` default (`COMMUNITY_URL` in `papro.py`).

## Review
Open `https://<worker>/admin?token=<ADMIN_TOKEN>`: approve, reject, remove, or download a pack to listen first.
Set `AUTO_APPROVE = "1"` in `wrangler.toml` and redeploy to publish without review (you can still remove packs).

## What the service checks
Only the pack layout (`main`, `dep`, `dest`, `fuel`, `time`, `extra`, `voices`, `airports` + `voice.json`, `logo.png`, `scripts.json`, `languages.json`), only `.ogg` audio inside, no unsafe paths, max 90 MB, and a simple tick from the author that they may share it. No licence is attached. PAPro checks the zip's SHA-256 again before installing.
Authors prove a pack is theirs with a private key PAPro keeps on their PC, so they can update or withdraw it without an account.
