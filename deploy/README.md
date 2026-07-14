# Guitar Tutor Copilot — go-live runbook

Plan 8 (`docs/superpowers/plans/2026-07-09-async-generation-and-deploy.md`), Task 7.
Tasks 1-6 are done: async curriculum generation is e2e-verified, and this `deploy/`
directory (nginx vhosts, the compose build-arg override, this runbook) has been authored
and build-verified — **no live system changes have been made yet.** This file is what's
left: an ordered, owner-tagged checklist to actually go live. Should take about 10 minutes
of hands-on time plus TLS/DNS propagation waits.

Full background/reasoning: `.superpowers/sdd/deploy-recon.md` (the original recon) and
`.superpowers/sdd/task-6-report.md` (what was built for this task, and why — the CSP
design, the prod-build verification, how the nginx configs were syntax-checked without
touching the live host).

**Target architecture** (matches the live `themis`/`zelofood` sibling apps on this box):

```
Internet -> Cloudflare edge (orange-cloud: TLS, DDoS, caching) -> home IP:443
         -> host nginx (one vhost per domain, by server_name)
         -> docker (127.0.0.1:8790 web, 127.0.0.1:8791 api)
```

Both `guitar.cgrigoriadis.online` (web) and `guitar-api.cgrigoriadis.online` (api) are
**orange-clouded**. This was previously an open question (Cloudflare's edge proxy has a
non-adjustable ~100-120s timeout, and `POST /curricula/generate` used to block for
49-179s) — it's resolved now: Plan 8 made generation async (`202` + `GET /jobs/{id}`
poll), so nothing this app does synchronously runs anywhere near that ceiling anymore.
Ordinary proxy timeouts, ordinary Cloudflare settings, no grey-cloud needed.

Owner tags used below: **[safe]** = no live effect, can run any time · **[owner: sudo]**
= needs a local sudo password · **[owner: Cloudflare]** = touches DNS/CF dashboard/token ·
**[owner: router]** = touches home-router config.

---

## Step 0 — sanity-check the loopback ports [safe]

Both containers are already running (verify only if it's been a while since you last
checked):

```bash
curl -I http://127.0.0.1:8790/                 # web -> expect 200 (or a redirect)
curl -s http://127.0.0.1:8791/health/live       # api -> {"status":"ok"}
```

---

## Step 1 (a) — DNS: add both subdomains, orange-clouded [owner: Cloudflare]

Uses the existing DDNS tool (`/home/chris/dns_resolution/`) — appends to `ddns.conf`'s
`RECORDS` array and runs the sync tool. **Never read/print the Cloudflare token in
`ddns.conf`** — append blind, exactly like every prior subdomain on this box.

```bash
cd /home/chris/dns_resolution
cat >> ddns.conf <<'EOF'

RECORDS+=(
  "cgrigoriadis.online|guitar.cgrigoriadis.online|true"
  "cgrigoriadis.online|guitar-api.cgrigoriadis.online|true"
)
EOF
./update-dns.sh --force
```

Verify both resolve to a Cloudflare anycast IP (not your home IP directly — that's what
`proxied=true`/orange-cloud means):

```bash
dig +short guitar.cgrigoriadis.online @1.1.1.1
dig +short guitar-api.cgrigoriadis.online @1.1.1.1
```

- `./update-dns.sh verify` printing `401` on the *account* check is expected/harmless for
  a DNS-scoped token — `--force`'s own `created -> <ip> (proxied=true)` output is the real
  signal.
- Both names are single-level subdomains (`guitar`, `guitar-api`) — required for Cloudflare
  Universal SSL (Free) to cover them; already true here, nothing to adjust.

---

## Step 2 (b) — confirm the router still forwards 80/443 here [owner: router]

No new port-forwarding rule is expected: `themis`, `zelofood`, `cglabs`, and `imatter`
already reach this exact host on 80/443 through the same router today, and that's
shared, not per-app, config. This step is a quick confirmation, not new setup — the
definitive proof is step 7's external HTTPS check succeeding. If you want to check now,
before DNS/certs are live: confirm in the router admin UI that 80 and 443 still forward
to this box's LAN IP, or use an external port-checker against your home public IP.

---

## Step 3 (c) — build the prod web image [safe]

`NEXT_PUBLIC_API_BASE` is a Next.js public env var — inlined into the client JS bundle
at **build time** (see `apps/web/Dockerfile`'s own comment; a running container's env
vars are too late for code already sent to the browser). `deploy/docker-compose.public.yml`
overrides just this one build arg for the `web` service; nothing else about the stack
changes.

This exact build was already run and verified as part of authoring this runbook (see
`task-6-report.md`): it succeeded, and the resulting image's `.next/static/` chunks were
grepped to confirm `https://guitar-api.cgrigoriadis.online` is really baked in (and the
dev default `localhost:8791` is not). **Rebuild fresh here anyway** rather than reusing
that verification image — code may have moved on since then:

```bash
cd /mnt/nvme2TB/guitar_tutor
docker compose -f docker-compose.yml -f deploy/docker-compose.public.yml build web
```

(Equivalent to `docker build --build-arg NEXT_PUBLIC_API_BASE=https://guitar-api.cgrigoriadis.online apps/web`,
just via the compose override — see that file's own header comment.)

Sanity-check the bake worked before swapping anything in:
```bash
docker compose -f docker-compose.yml -f deploy/docker-compose.public.yml run --rm --no-deps \
  --entrypoint sh web -c "grep -rl guitar-api.cgrigoriadis.online .next/static/ | head -3"
```
Expect at least one matching file. If nothing matches, stop — do not proceed to step 4.

---

## Step 4 (d) — swap the web container in; make sure api is current [safe]

```bash
cd /mnt/nvme2TB/guitar_tutor

# Web: recreate from the just-built prod-configured image (build+up in one step is
# also fine if you skipped the separate build above).
docker compose -f docker-compose.yml -f deploy/docker-compose.public.yml \
  up -d --build --no-deps web

# API + worker: no prod-specific config needed (CORS already allows the prod web
# origin by default — apps/api/app/config.py's cors_origins — and there's no
# build-time-baked value on this side), but rebuild anyway so they reflect whatever
# was last committed, same hygiene as the web side.
docker compose up -d --build --no-deps api worker
```

Confirm the swap actually took (a stale container masquerading as "deployed" is the
single most common deploy mistake — see deploy-recon.md / the cloudflare playbook's own
"gotchas" table):
```bash
docker inspect guitar_tutor-web-1 --format '{{.Created}}'
git log -1 --format='%h %cd'   # web's Created should be AFTER your last relevant commit
curl -I http://127.0.0.1:8790/
curl -s http://127.0.0.1:8791/health/live
```

---

## Step 5 (e) — install the nginx vhosts [owner: sudo]

```bash
cd /mnt/nvme2TB/guitar_tutor
sudo cp deploy/nginx/guitar.conf     /etc/nginx/sites-available/guitar.conf
sudo cp deploy/nginx/guitar-api.conf /etc/nginx/sites-available/guitar-api.conf
sudo ln -sf /etc/nginx/sites-available/guitar.conf     /etc/nginx/sites-enabled/guitar.conf
sudo ln -sf /etc/nginx/sites-available/guitar-api.conf /etc/nginx/sites-enabled/guitar-api.conf
sudo nginx -t && sudo systemctl reload nginx
```

Both vhosts are HTTP-only (`listen 80`) on purpose, so `nginx -t` passes before a TLS
cert exists — that was already confirmed structurally in a throwaway, non-live Docker
container during authoring (see `task-6-report.md`); `nginx -t` here is the real
authoritative check against your actual `/etc/nginx` tree (global `conf.d/` hardening,
the real `snippets/cloudflare-origin-allow.conf`, etc.) and must still pass before you
reload.

---

## Step 6 (f) — TLS via certbot [owner: sudo]

One combined certificate for both names, in a single invocation — this is the exact
pattern already live for `themis`/`themis-api` (both of those vhosts reference
`/etc/letsencrypt/live/themis.cgrigoriadis.online/...`, confirmed by reading them
directly):

```bash
sudo certbot --nginx -d guitar.cgrigoriadis.online -d guitar-api.cgrigoriadis.online
```

This rewrites both vhost files in place: adds the `listen 443 ssl` server block +
`ssl_certificate`/`ssl_certificate_key` pointing at
`/etc/letsencrypt/live/guitar.cgrigoriadis.online/`, plus an HTTP->HTTPS redirect server
on port 80. (Your `deploy/nginx/*.conf` copies in the repo are the **pre-certbot**
originals — expected to diverge from `/etc/nginx/sites-available/*` after this step;
that's the same pattern every sibling vhost already follows.)

If HTTP-01 fails (e.g. Cloudflare's Always-Use-HTTPS pre-empts the `:80` challenge before
the cert exists), fall back to DNS-01:
```bash
sudo certbot certonly --dns-cloudflare --dns-cloudflare-credentials <creds.ini> \
  -d guitar.cgrigoriadis.online -d guitar-api.cgrigoriadis.online
```

Cache rules (`cf-cache-rules.sh cgrigoriadis.online`) are applied **per zone, not per
subdomain**, and already active for `cgrigoriadis.online` (every sibling vhost benefits
from it already) — nothing new to run here.

---

## Step 7 (g) — verify, end to end, through Cloudflare [safe]

```bash
# HTML is real and NOT edge-cached (must say DYNAMIC, not HIT):
curl -s -D - -o /tmp/guitar_body.html https://guitar.cgrigoriadis.online/ \
  | grep -iE "cf-cache-status|cache-control"
head -c 20 /tmp/guitar_body.html          # expect: <!DOCTYPE html>

# API reachable through its own vhost:
curl -s https://guitar-api.cgrigoriadis.online/health/live   # {"status":"ok"}

# CSP present and correct (compare against deploy/nginx/guitar.conf / task-6-report.md):
curl -s -D - -o /dev/null https://guitar.cgrigoriadis.online/ | grep -i content-security-policy

# _next/static assets cache forever at the edge:
A=$(curl -s https://guitar.cgrigoriadis.online/ | grep -oE '/_next/static/[^"]+\.(js|css)' | head -1)
curl -s -D - -o /dev/null "https://guitar.cgrigoriadis.online$A" | grep -iE "cf-cache-status|cache-control"

# Origin lockdown: direct-to-home-IP must NOT be answered (only Cloudflare edge IPs are).
```

Then, in a real browser (this is the part curl can't cover):

1. Open `https://guitar.cgrigoriadis.online` — cockpit shell loads, no mixed-content/CSP
   errors in the console.
2. Click through **Knowledge**, **Students**, **Curricula**, **Artifacts** — each talks
   directly to `https://guitar-api.cgrigoriadis.online` from the browser (open DevTools
   Network tab and confirm requests actually go to that origin, not `localhost`).
3. Trigger a **real curriculum generation** (Curricula -> Generate): confirm it returns
   immediately (202-style non-blocking UI), the poll loop runs, and it eventually
   completes and renders the tree — this is the specific behavior that used to be at risk
   of a Cloudflare 524 and is the main thing to prove works end-to-end through the CF
   proxy now.
4. Open or attach a **tab-kind artifact** and confirm: notation renders, and the Play
   button becomes enabled and actually plays audio. Check the browser console for any
   `Refused to ... because it violates the following Content Security Policy directive`
   messages — zero such messages is the definitive CSP-is-correct signal (this exercises
   the blob-URL worker + same-origin font/soundfont fetch all at once).

Also confirm the container that's actually serving traffic is the one you just built:
```bash
docker inspect guitar_tutor-web-1 --format '{{.Created}}'
git -C /mnt/nvme2TB/guitar_tutor log -1 --format='%h %cd'
```

---

## ⚠️ Never rebuild `web` or `api` for prod without the override

```bash
# ALWAYS, for anything that touches the deployed site:
docker compose -f docker-compose.yml -f deploy/docker-compose.public.yml up -d --build web api
```

A plain `docker compose up -d --build web` **silently breaks the live site**, and
the failure is invisible from the server:

- `NEXT_PUBLIC_API_BASE` is inlined into the **client bundle at build time**. The
  base compose bakes `http://localhost:8791`, so the deployed JavaScript tells
  **the visitor's browser to call the visitor's own localhost**. Every API call
  fails with a connection error; the API logs show nothing, because nothing ever
  arrives. The user sees *"Couldn't reach the server."*
- Since the password gate, `api` needs the override too: `COOKIE_DOMAIN` must be
  `.cgrigoriadis.online` so one cookie covers **both** `guitar.` and
  `guitar-api.`. A host-only cookie authorises the API fine but is invisible to
  the Next.js middleware on the other subdomain — which reads it to decide
  whether to bounce to `/login`. The result is a redirect loop for a user who is
  actually logged in.

Verify the bake before trusting a deploy:

```bash
docker compose exec -T web sh -c \
  'grep -rho "https://guitar-api[a-z0-9.-]*\|http://localhost:8791" .next/static/chunks/*.js | sort -u'
# MUST print: https://guitar-api.cgrigoriadis.online
```

And the one that is easy to forget, because it works right up until it doesn't:

```bash
# the Reader's <img> cannot send an Authorization header — it must work on the
# cookie alone, AND must not be edge-cached by Cloudflare
curl -sI -b jar.txt https://guitar-api.cgrigoriadis.online/media/pages/<page_id>.jpg \
  | grep -iE 'HTTP|cache-control|cf-cache-status'
# MUST show: 200 · cache-control: private, no-store · cf-cache-status: DYNAMIC
# Without `no-store`, Cloudflare caches the tutor's scanned book and serves it to
# anyone with a page id — cookie or not. The gate becomes theatre.
```

---

## Rollback

- **nginx**: `sudo rm /etc/nginx/sites-enabled/guitar.conf /etc/nginx/sites-enabled/guitar-api.conf && sudo nginx -t && sudo systemctl reload nginx`
  (leaves `sites-available/` + certs in place for next time).
- **web container**: `docker compose up -d --build --no-deps web` (no `-f deploy/docker-compose.public.yml`)
  rebuilds with the dev-default `NEXT_PUBLIC_API_BASE=http://localhost:8791` from `.env`,
  restoring the pre-deploy state.
- **DNS**: remove the two `RECORDS+=` lines from `ddns.conf` and re-run
  `./update-dns.sh --force`, or simply leave the records — an orange-clouded record with
  nothing behind it just stops resolving usefully, it doesn't expose anything.

## Files in this directory

- `nginx/guitar.conf` — web vhost (`guitar.cgrigoriadis.online` -> `127.0.0.1:8790`), CSP included.
- `nginx/guitar-api.conf` — api vhost (`guitar-api.cgrigoriadis.online` -> `127.0.0.1:8791`).
- `docker-compose.public.yml` — build-arg override, `NEXT_PUBLIC_API_BASE` only.
