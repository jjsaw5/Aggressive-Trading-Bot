# Setup

## Quick start — no credentials needed

```bash
pip install -e ".[dev]"
alembic upgrade head          # creates the ma_* tables
python run_market_scan.py
```

Providers default to the mock stack; agents default to a credential-free
deterministic runner. Both are stamped on the report and every stored row.

## CLI

```bash
python run_market_scan.py                        # full pipeline, audit shown
python run_market_scan.py --stage premarket      # research only, no contracts
python run_market_scan.py --symbols NVDA,AMD,MU  # a specific list
python run_market_scan.py --no-audit             # ranked report without per-rule trace
python run_market_scan.py --json report.json     # machine-readable alongside
python run_market_scan.py --no-persist           # do not write to the database
python run_market_scan.py --runner anthropic     # needs ANTHROPIC_API_KEY
python run_market_scan.py --methodology alt.yaml # alternate rubric
python run_market_scan.py --at 2026-08-05T16:00:00Z   # run as-of an instant
```

`--at` is how you exercise the market-open path outside trading hours, and how
you reproduce a past scan. Without it, a `full` run while the options market is
closed is **downgraded to premarket** — see
[ARCHITECTURE.md](ARCHITECTURE.md#premarket-vs-market-open).

## API

```bash
uvicorn app.main:app --reload
```

| Endpoint | Purpose |
|---|---|
| `POST /multiagent/scans` | run a scan, return a summary |
| `POST /multiagent/scans/report` | run a scan, return the full report |
| `POST /multiagent/scans/text` | run a scan, return the rendered console report |
| `GET /multiagent/runs` | recent runs |
| `GET /multiagent/runs/{id}/recommendations` | ranked **and** rejected |
| `GET /multiagent/candidates/{id}/audit` | every point, traced to its measurement |
| `GET /multiagent/methodology` | the live rubric |
| `POST /multiagent/decisions` | approved / rejected / watched / entered / skipped |
| `POST /multiagent/executions` | record a trade **you** entered |
| `POST /multiagent/results` | record the outcome |

Interactive docs at `/docs`. There is no order-placement endpoint.

## Claude Code subagents

`.claude/agents/` holds the three pipeline agents plus a defined-but-unwired
risk reviewer. In a Claude Code session they are available as subagents; the
Python runtime loads the same files as system prompts. One source of truth, so
the anti-hallucination rules cannot drift between the two paths.

## Configuration

| File | Governs | Change means |
|---|---|---|
| `config/methodology.yaml` | weights, thresholds, bands, allowed strategies | what a score *means* — bump `version` |
| `.env` | credentials, provider routing, runner choice | where data comes from |

Runtime variables for this subsystem (all optional):

```bash
MA_AGENT_RUNNER=deterministic     # or "anthropic"
MA_ANTHROPIC_MODEL=claude-sonnet-5
ANTHROPIC_API_KEY=...             # only for the anthropic runner
MA_USE_RESEARCH_MOCK=true         # richer synthetic news when providers are mocked
MA_PERSIST=true
MA_METHODOLOGY_PATH=              # override the methodology file
```

## Enabling live providers

Add the key to `.env`, then point the capability at the vendor:

```bash
FMP_API_KEY=...
PROVIDER_MARKET_DATA=fmp
PROVIDER_FUNDAMENTALS=fmp
PROVIDER_CALENDAR=fmp

UW_API_KEY=...
PROVIDER_OPTIONS_FLOW=unusual_whales
PROVIDER_OPTIONS_CHAIN=unusual_whales
PROVIDER_IV_HISTORY=unusual_whales
```

Robinhood (read-only) needs `pip install -e ".[robinhood]"` and `RH_*`.
See [DATA_SOURCES.md](DATA_SOURCES.md) for status by vendor.

## Enabling the LLM runner

```bash
pip install -e ".[llm]"
echo 'ANTHROPIC_API_KEY=sk-ant-...' >> .env      # .env is gitignored
python run_market_scan.py --runner anthropic
```

A missing SDK or key is a loud, specific error — never a silent fallback to the
deterministic runner, because a corpus whose stated author is wrong is worse
than a run that did not happen.

## Database

Defaults to whatever `app/db/session.py` resolves: Turso if configured,
otherwise `DATABASE_URL`, otherwise Postgres. Local SQLite is fine:

```bash
export DATABASE_URL="sqlite:///./atb.db"
alembic upgrade head
```

Migration `0007_multiagent_research` creates 18 `ma_*` tables. It is
`CREATE TABLE` only — no existing table is altered — and it downgrades cleanly.

## Tests

```bash
pytest tests/multiagent -q     # this subsystem
pytest -q                      # everything
ruff check .
```

`tests/multiagent/test_freeze_isolation.py` is the one to watch. It proves this
subsystem does not disturb the frozen short-duration scoring model. If it fails,
the question is not "how do I make this pass" but "does this change end the
capture window?" (`CLAUDE.md` §2).

## Secrets

`.env` is gitignored. Never commit, print, or paste a credential — not into a
log line, a prompt, a report, a database row, a PR body or a chat transcript. A
key written down anywhere but a secret store is already compromised; rotate it
rather than reasoning about who saw it.

Secret-scan every staged diff. CI runs gitleaks as a backstop, not as the first
line.
