# Go-Live Runbook

How to run the platform live so it monitors the market and your positions, and
surfaces trades for you to take. **It never places orders** — automation is off
by a hard double-gate; you execute in your broker.

## 0. Prerequisites

- A **persistent host** (VM/box you control). A laptop that sleeps or an
  ephemeral container will NOT keep the scheduler alive — it must stay up.
- Python 3.11+ and the repo checked out.
- A `.env` with your secrets (never committed). See the checklist below.

## 1. `.env` checklist

```
# Providers (live)
PROVIDER_MARKET_DATA=fmp
PROVIDER_FUNDAMENTALS=fmp
PROVIDER_CALENDAR=fmp
PROVIDER_OPTIONS_FLOW=unusual_whales
PROVIDER_IV_HISTORY=unusual_whales
PROVIDER_OPTIONS_CHAIN=unusual_whales
FMP_API_KEY=...                 # or set in the host environment
UNUSUAL_WHALES_API_KEY=...

# Durable storage
TURSO_DATABASE_URL=libsql://<db>.turso.io
TURSO_AUTH_TOKEN=...

# Scheduler: session-aware tiered funnel
TIERING_ENABLED=true
SESSION_TICK_SECONDS=10

# Alerts to your phone
ALERTS_ENABLED=true
ALERTS_CHANNEL=slack
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...   # REQUIRED for phone alerts
ALERTS_MIN_SCORE=0.55

# Efficiency (defaults are fine)
CACHE_ENABLED=true
RATE_LIMIT_ENABLED=true
```

If `SLACK_WEBHOOK_URL` is unset, alerts fall back to console (host logs only).

## 2. Run with Docker (recommended)

The compose stack builds one image and runs two containers — `api` (dashboard on
:8000) and `scheduler` (the tiered funnel). No Postgres/Redis: storage is Turso
and the cache is in-process. Secrets come from `.env` at runtime and are never
baked into the image (`.dockerignore` excludes `.env`).

```
docker compose up -d --build          # build + start both containers
docker compose logs -f scheduler      # watch the funnel tick
docker compose ps                     # api should be "healthy"
```

Open **http://localhost:8000/dashboard**. To stop: `docker compose down`
(add `restart: unless-stopped` keeps them running across reboots — already set).

Tables are created automatically on startup — no migration step.

### Manual alternative (no Docker)

```
pip install -e ".[dev]"
python -c "from app.db.session import create_all; create_all()"   # idempotent
```

## 3. Run the two processes (manual only)

Run both under a supervisor (systemd, pm2, tmux, or `docker compose`) so they
restart on crash and survive logout.

```
# API + dashboard (Ops, Positions, Calibration)
uvicorn app.main:app --host 0.0.0.0 --port 8000

# Scheduler — drives the tiered funnel at session cadences (TIERING_ENABLED=true)
python -m app.scheduler.run
```

The scheduler picks the **session-aware** path automatically when
`TIERING_ENABLED=true`; otherwise it runs the simple periodic scan.

## 4. Confirm it's alive (run before the open)

```
curl -s localhost:8000/health
curl -s localhost:8000/config/providers   | jq '.[].provider'     # fmp / unusual_whales
curl -s localhost:8000/positions          | jq '.[] | {symbol,action,pnl_usd}'
curl -s localhost:8000/metrics            | jq '.cache, .rate_limit.enabled'
```

Dashboard: open `http://<host>:8000/dashboard`
- **Positions** tab — your holdings, live P&L, and the mechanical exit action
  (stops/take-profits sorted to the top). Auto-refreshes every 5s.
- **Ops** tab — provider latency/errors, cache hit rate, tier sizes, funnel
  timings. Auto-refreshes.
- **Candidates** tab — new setups the funnel surfaced (with thesis + ticket).

## 5. Friday-morning sequence (market open)

1. ~9:00 ET: confirm both processes are up (step 4). Session should read
   `pre_market` → `final_pre_open`.
2. Check the **Positions** tab. Act on anything flagged `stop` / `take_profit`
   / `time_stop` (e.g. a 0-DTE that tripped its stop).
3. 9:30 open: the scheduler moves to `opening` → `primary` and tightens cadence
   (watchlist 1 min, positions ~20 s). New actionable setups arrive as Slack
   alerts and on the Candidates tab.
4. When you take a trade: place it in your broker, then log it so Tier 4 tracks
   it — `POST /paper/import` with the legs (or `POST /paper` for a
   funnel-surfaced candidate).

## 6. Keeping tracked positions in sync

The app can't read your broker directly in every environment, so tracked
positions are a snapshot. Re-import after you open/close trades:

```
curl -s -X POST localhost:8000/paper/import -H 'content-type: application/json' -d '[
  {"symbol":"NVDA","legs":[
    {"strike":210,"option_type":"call","is_long":true,"quantity":1,"entry_price_per_share":10.75,"expiration":"2026-08-21"},
    {"strike":215,"option_type":"call","is_long":false,"quantity":1,"entry_price_per_share":8.50,"expiration":"2026-08-21"}]}
]'
```

## 7. Safety invariants (do not change casually)

- `AUTOMATION_ENABLED=false` and `TRADING_MODE=research` — the execution guard
  refuses to place orders regardless of anything else. You are the trigger.
- Defined risk only: every surfaced structure has a bounded max loss within your
  per-trade cap.
- Secrets live only in `.env` (gitignored). Never commit them.

## Known follow-ups

- **Broker auto-sync**: positions are imported manually (the `robin_stocks`
  client doesn't run in all environments). A working broker provider would let
  Tier 4 reconcile automatically.
- **Redis**: optional; in-memory cache/bus is the default and is sufficient for
  a single scheduler process.
