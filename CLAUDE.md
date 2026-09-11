# beachsync — Claude Code notes

TigerBay -> HubSpot profile sync. TigerBay fires a webhook on customer / agent-staff
created / modified / archived; beachsync (FastAPI + SQLite queue + worker thread) stores the
event, re-reads the profile from the TigerBay Nimble API, diffs it against HubSpot and writes
only what changed. LIVE since 2026-09-09. Details, field mapping and admin API: README.md.

## Hard rules (do not relax without the user)
- **Drift sweep is report-only.** `bin/sweep_drift.sh` runs `python -m app.sweep drift` with
  NO `--queue`. On 2026-09-10 a queuing run overwrote ~14k hand-curated HubSpot records and
  was reverted (`app.revert_sweep`, `app.fix_sweep_duplicates`). Never add `--queue` to the
  script or crontab, never add a `backfill all` cron: curated HubSpot data changes only via
  real TigerBay webhooks (plus the 02:40 new-ids sweep, which creates only).
- **HubSpot rate budget is per account**, shared by every integration. beachsync uses
  `HUBSPOT_RATE_PER_10S=80`; if another HubSpot integration appears, lower it in `.env`.
- **TigerBay has four tenants** (preview, candidate, pre-production, production), each with
  its own client_id/secret. Production is `beachcomber.ontigerbay.co.uk/nimble` (in `.env`);
  the pre-production key is the current non-prod one; the preview key was deleted 2026-09-08.
  Non-prod tenants are anonymised and force dry-run. Never mix a key with another tenant's URL.
- **DNS from this box** only works via 1.1.1.1 / 9.9.9.9 (firewall); authoritative servers
  and 8.8.8.8 time out. Verify names with `dig @1.1.1.1`.
- Secrets live in `secrets/*.txt` and `.env` (gitignored). Never print or commit them.

## Container, deploy, restart
- Own compose project (`docker-compose.yml`), container `beachsync`, port 8080 exposed only
  on the external docker network `edge`; nothing published on the host. State: `data/beachsync.db`
  (queue + TigerBay->HubSpot id map) via `./data:/data`.
- Deploy a change: `scripts/deploy.sh "message"` (pytest -> `docker compose up -d --build` ->
  health gate with rollback to the previous image -> commit + push). `--no-push` skips git,
  `--check` runs tests + health only. Plain restart: `docker compose restart`.
- Health: `curl -s http://$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' beachsync):8080/health`
- Edge proxy (`/home/bctadmin/edgerunner/deploy/nginx`, container `edge-nginx`): vhosts in
  `conf.d/beachsync.conf` (public) and `conf.d/beachsync-lan.conf` (LAN); repo copies in
  `deploy/`. Public name is `beachsync.beachcomberapi.uk` (Cloudflare-proxied, Cloudflare
  Origin CA wildcard cert); only `/webhooks/*` and `/health` are exposed. Reload after a vhost
  change: `docker exec edge-nginx nginx -t && docker exec edge-nginx nginx -s reload`.

## Cron (user crontab, all in `bin/`, logs in `logs/`)
| When | Script | Does |
|---|---|---|
| `*/15 * * * *` | `check_health.sh` | silent on ok; Teams alert on down / worker dead / dry-run / new failed / unparsed / backlog / no webhook 7d |
| `40 2 * * *` | `sweep_new_ids.sh` | queues TigerBay records created above the last id seen (creates only) |
| `15 3 * * *` | `backup_db.sh` | SQLite online backup to `backups/`, 30 days |
| `0 4 * * *` | `sweep_drift.sh` | full reconciliation, **report-only**, CSV in `data/`, Teams rollup |
| `30 7 * * *` | `daily_digest.sh` | yesterday's change digest to Teams |
| `0 8 * * 1` | `daily_digest.sh 7d` | weekly digest |

Teams webhook URL: `.alert_webhook` (falls back to `/home/bctadmin/beachstats/.alert_webhook`).

## Tests
`python3 -m pytest -q tests` (in-memory fakes of both APIs; 68 tests, ~2 s). `deploy.sh` runs
them first and stops on failure.

## Dashboard and digest (LAN/tailnet only: https://beachsync.bctuk.com)
`/status` (+ `/status.json`, pause/resume kill switch), `/events` (queue with filters),
`/sweeps` (every new-id / drift run and its CSV), `/digest?window=today|yesterday|7d|30d`
(+ `/digest.json`, what was written to HubSpot). Admin API `/admin/*` (header `X-Admin-Token`)
is reachable only from the box or the `edge` network; see README for the routes.

## Resuming work
Check `/health` and `GET /admin/events?status=failed` first. Memory notes with the go-live
decisions and API quirks: `~/.claude/projects/-home-bctadmin-beachsync/memory/`.
