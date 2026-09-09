# beachsync — TigerBay → HubSpot profile sync

Keeps HubSpot contacts (and companies) in step with TigerBay customer and
agent-staff profiles. TigerBay fires a webhook on **created / modified /
archived**; beachsync stores the event, re-reads the full profile from the
TigerBay Nimble API, diffs it against the HubSpot record, and writes only what
changed.

```
TigerBay ──webhook──▶ beachsync ──GET profile──▶ TigerBay Nimble API
                        │
                        └──lookup / diff / PATCH or POST──▶ HubSpot CRM v3
```

## Webhook URLs to configure in TigerBay

| Entity | Event | URL |
|---|---|---|
| Customer | Created | `POST https://beachsync.beachcomberapi.uk/webhooks/tigerbay/customer/created` |
| Customer | Modified | `POST https://beachsync.beachcomberapi.uk/webhooks/tigerbay/customer/modified` |
| Customer | Archived | `POST https://beachsync.beachcomberapi.uk/webhooks/tigerbay/customer/archived` |
| Agent staff | Created | `POST https://beachsync.beachcomberapi.uk/webhooks/tigerbay/agent/created` |
| Agent staff | Modified | `POST https://beachsync.beachcomberapi.uk/webhooks/tigerbay/agent/modified` |
| Agent staff | Archived | `POST https://beachsync.beachcomberapi.uk/webhooks/tigerbay/agent/archived` |

If TigerBay only allows one URL per entity, use `/webhooks/tigerbay/customer`
and `/webhooks/tigerbay/agent`; the event is then read from the payload (and an
`Archived: true` in the body is honoured regardless).

**Auth** (configure either or both in TigerBay; the receiver accepts either):

- HTTP Basic: user `WEBHOOK_BASIC_USER` (default `tigerbay`), password from `secrets/webhook_basic_password.txt`
- Header: `X-Webhook-Token: <secrets/webhook_header_value.txt>` (header name is `WEBHOOK_HEADER_NAME`)

**Payload**: TigerBay's webhook body format is not publicly documented, so the
parser is tolerant: it looks for the record id under `EntityId`, `CustomerId`,
`AgentStaffId`, `Id`, `ID`, `BundleReference` (`22926-1110` → 22926), a self
`Href`, nested objects, `?id=` on the query string, or a trailing `/{id}` path
segment. A delivery whose id cannot be found is stored with status `unparsed`
and answered `400`, so the first real deliveries can be inspected with
`GET /admin/events?status=unparsed` and the parser adjusted in `app/webhook.py`.

## What gets synced

TigerBay endpoints used (verified against the pre-production tenant):

| TigerBay | HubSpot |
|---|---|
| `GET /sales/customers/{id}` + `/contacts` (Primary) | **Contact**, `brand_channels=Direct` |
| `GET /sales/agents/{id}` with `Type=Staff` + `/contacts` + parent agency | **Contact**, `brand_channels=Travel Agent` |
| `GET /sales/agents/{GroupId}` with `Type=Agent` | **Company** (optional, `SYNC_AGENT_COMPANIES`, off by default) |

The HubSpot portal already holds a TigerBay export (Aug 2026), so the mapping
targets that export's properties. Field mapping lives in [`app/mapping.py`](app/mapping.py).

| HubSpot property | Customer source | Agent-staff source |
|---|---|---|
| `email` | `EmailAddress` (fallback primary contact) | staff `Email` (fallback contact / `Reference`) |
| `firstname` / `lastname` | `Forename` / `Surname` | primary contact or split `Name` |
| `title` | `Title` | contact `Title` |
| `phone` / `mobilephone` | contact landline / mobile | business landline / mobile |
| `address`, `city`, `county`, `zip` | primary contact | primary contact |
| `tigerbay_id` | customer `Id` | **agency** `GroupId` (export convention) |
| `tigerbay_customer_id` (unique, new) | customer `Id` | – |
| `tigerbay_agent_id` (unique, new) | – | staff `ID` |
| `agency_name` / `abta_reference` | – | agency `Name` / `Reference` |
| `cancel_from_email` / `cancel_from_mailing` | `DoNotEmail` / `DoNotMail` as TRUE/FALSE | – |
| `is_archived` | `Archived` as TRUE/FALSE | `IsArchived` |
| `source_last_modified` | today, on every real change | same |
| create only: `brand_channels`, `original_data_source=Tigerbay`, `lifecyclestage` (`HUBSPOT_*_LIFECYCLE`, default `lead`), `source_created_date`, `company` (staff) | | |

**Opt-outs:** `cancel_from_email` / `cancel_from_mailing` are one-way: TigerBay
can set an opt-out but never clears one HubSpot already holds.

**Email policy:** a webhook event (a TigerBay create/modify from go-live
onwards) updates the HubSpot email from TigerBay. Backfill/reconciliation
events (`source=backfill`) never replace an existing HubSpot email; they only
fill an empty one.

Address/phone/title/agency fields that are blank in TigerBay are **not** blanked
in HubSpot (`NEVER_CLEAR`). Everything not listed is never touched (`date_of_birth` and `country` deliberately excluded).

Record matching order: local id map → unique `tigerbay_customer_id` /
`tigerbay_agent_id` → legacy export row by `tigerbay_id` (customers only,
exact and unambiguous, excluding Travel Agent rows) → email (only if that
contact is not bound to a different TigerBay id). Hits are re-verified with a
direct GET.

**Archived** in TigerBay (customer or agent staff) → `is_archived=TRUE` by default
(`HUBSPOT_ARCHIVE_ACTION=flag`); `delete` moves the record to the recycle bin. The flag is applied when the
`archived` webhook fires, and also whenever a re-read record carries
`Archived`/`IsArchived`. If TigerBay stops serving an archived record (404), an
`archived` event still flags the HubSpot record found by its TigerBay id.

**Agency archived** → every staff contact of that agency is flagged: those
TigerBay still lists are re-synced with the archived flag, and any remaining
HubSpot Travel Agent contact carrying that agency's `tigerbay_id` is flagged
directly. A staff member whose agency is archived is also treated as archived
whenever their own record is synced.

**Safety net:** non-production TigerBay tenants (preview, candidate,
pre-production) contain anonymised data. If `TIGERBAY_BASE_URL` points at one,
writes are forced to dry-run regardless of `DRY_RUN`, unless
`ALLOW_WRITES_FROM_NONPROD_TIGERBAY=true`.

## Running

```bash
cp .env.example .env            # fill in TIGERBAY_BASE_URL/TOKEN_URL/CLIENT_ID
printf '%s' '<tigerbay client secret>' > secrets/tigerbay_client_secret.txt
printf '%s' '<hubspot private app token>' > secrets/hubspot_access_token.txt
openssl rand -hex 24 > secrets/webhook_basic_password.txt
openssl rand -hex 24 > secrets/webhook_header_value.txt
openssl rand -hex 24 > secrets/admin_token.txt
chmod 600 secrets/*.txt
docker compose up -d --build
curl -s http://$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' beachsync):8080/health
```

HubSpot private app scopes: `crm.objects.contacts.read/write`,
`crm.objects.companies.read/write`, `crm.schemas.contacts.write`,
`crm.schemas.companies.write`.

Start with `DRY_RUN=true` for the first backfill: every diff is logged and
stored on the event row, nothing is written to HubSpot.

### Public exposure (edge proxy)

1. Add a public DNS **A record** `beachsync.beachcomberapi.uk → 195.224.77.34` (GoDaddy, same zone as `netbird.beachcombertours.uk`).
2. `cp deploy/nginx-beachsync.conf /home/bctadmin/edgerunner/deploy/nginx/conf.d/beachsync.conf`
3. `deploy/issue-cert.sh` (creates a temp self-signed pair, reloads nginx, issues the Let's Encrypt cert via the shared acme webroot, installs it, reloads again). Renewal is handled by the existing acme.sh cron.
4. `curl -s https://beachsync.beachcomberapi.uk/health`

Only `/webhooks/*` and `/health` are exposed through the proxy; `/admin/*` is reachable only from the box or the `edge` docker network.

### Admin API (header `X-Admin-Token: <secrets/admin_token.txt>`)

| | |
|---|---|
| `GET /admin/events?status=failed&limit=50` | queue inspection (`pending, processing, done, failed, skipped, unparsed`) |
| `GET /admin/events/{id}` | one event incl. raw webhook body and sync result |
| `POST /admin/events/{id}/retry` | re-queue a failed/done event |
| `POST /admin/events/retry-failed` | re-queue every failed event (after an outage) |
| `POST /admin/events/{id}/dismiss` | acknowledge a failed/unparsed event (stops it alerting) |
| `POST /admin/sync/{customer|agent}/{tigerbayId}` | queue a manual sync |
| `GET /admin/preview/{customer|agent}/{tigerbayId}` | dry-run one record now and return the diff |

### Backfill / reconciliation

```bash
docker compose exec beachsync python -m app.backfill agents      # all agencies + staff
docker compose exec beachsync python -m app.backfill customers   # walks customer ids 1..max
docker compose exec beachsync python -m app.backfill all
```

Events are queued with `source=backfill` and drained by the worker at the
configured HubSpot rate (~80 req/10 s). Re-running is safe; unchanged records
are no-ops. Consider a weekly cron of `backfill all` as a safety net for missed
webhooks.

### Deploying a change

```bash
scripts/deploy.sh "what changed"
```

Runs the tests (a failure stops here), rebuilds and restarts the container,
waits for `/health` to report ok with the worker alive (rolls back to the
previous image if not), then commits and pushes. `--no-push` skips git,
`--check` only runs tests and the health probe.

### Tests

```bash
python3 -m pytest -q tests
```

The suite uses in-memory fakes of both APIs with the real payload shapes.

## Monitoring and backups

- `bin/check_health.sh` (cron, every 15 min): silent on success; alerts to the
  Teams webhook in `beachstats/.alert_webhook` on container/worker down, dry-run
  on, new failed events, unparsed webhooks, a stale backlog, or no webhook from
  TigerBay for 7 days. Acknowledge a known failure with `/admin/events/{id}/dismiss`.
- `bin/backup_db.sh` (cron, nightly 03:15): SQLite online backup of
  `data/beachsync.db` into `backups/`, gzipped, 30 days kept. Restore = stop the
  container, gunzip over `data/beachsync.db` (delete any `-wal`/`-shm` sidecars
  first), start.

## Missed-webhook safety net

- `bin/sweep_new_ids.sh` (cron, nightly 02:40): finds TigerBay customers and
  staff with ids above the highest this service has seen and queues them as
  `created` (source `sweep`).
- `bin/sweep_drift.sh` (cron, daily 04:00): runs the full reconciliation and
  queues every matched record with an actionable difference as `modified`
  (source `sweep`, so an existing HubSpot email is kept). Records with no
  HubSpot match are not created by the sweep. Both sweeps post a rollup to the
  Teams alert channel (new-ids only when it found something).

## Status dashboard

`https://beachsync.bctuk.com/status` (LAN/tailnet only, wildcard cert, vhost in
`deploy/nginx-beachsync-lan.conf`): service state, last webhook, counts,
events per day, failures needing attention, recent events. Ids only, no
personal data. `/status.json` for the raw numbers.

**Kill switch** on the status page: *Pause sync* (one click, optional reason)
stops all HubSpot writes immediately and persists across restarts; webhooks keep
being accepted and queued so nothing is lost. *Resume* requires the admin token.
While paused the health check posts a reminder to Teams once an hour.

## Queue behaviour

- **Fan-out.** An event for an agency id queues one child event per staff
  member (`source=fanout`, `parent_event_id` set) instead of syncing them
  inline, so a large agency never blocks webhook traffic.
- **Duplicates.** TigerBay delivers every event twice. Once an event is
  processed, older pending events for the same record are marked
  `superseded` (never an `archived` event).
- **Retries.** Transient failures back off 15 s → 1 h for up to `MAX_ATTEMPTS`
  (30, roughly a day) before an event is `failed`. After a longer outage,
  `POST /admin/events/retry-failed` re-queues them all.

## Operational notes

- The SQLite file in `data/` is the durable queue and the TigerBay→HubSpot id
  map; back it up with the rest of the box.
- Retries: transient TigerBay/HubSpot errors back off 15 s → 1 h, up to
  `MAX_ATTEMPTS` (8), then the event is `failed` and visible in the admin API.
- HubSpot 429s are honoured (`Retry-After`); a daily-limit 429 fails the event
  for retry later.
- HubSpot moved to date-based API versions in 2026; the v3 paths used here
  remain supported. Update `app/hubspot.py` paths when migrating.
