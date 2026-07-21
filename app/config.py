"""Application configuration via Pydantic settings.

All configuration is sourced from environment variables (see `.env.example`).
Nothing in the codebase should read `os.environ` directly — import `settings`.
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache

from pydantic import Field, computed_field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class TradingMode(str, Enum):
    RESEARCH = "research"
    PAPER = "paper"
    APPROVAL = "approval"
    AUTOMATION = "automation"


class ProviderName(str, Enum):
    MOCK = "mock"
    FMP = "fmp"
    UNUSUAL_WHALES = "unusual_whales"
    ROBINHOOD = "robinhood"
    BENZINGA = "benzinga"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Runtime ---
    app_env: str = "local"
    log_level: str = "INFO"
    log_json: bool = False

    # --- Operating mode ---
    trading_mode: TradingMode = TradingMode.RESEARCH
    automation_enabled: bool = False

    # --- Scheduler ---
    # Baseline research-scan cadence (minutes). Default 180 (every 3 hours): the
    # scanner runs 24/7 and is not yet market-session-aware, so a slow baseline
    # keeps closed-market API waste low. Session-aware per-tier cadences arrive
    # with the scheduling clock; until then, tune this via SCAN_INTERVAL_MINUTES.
    scan_interval_minutes: int = 180

    # --- Event bus + change detection (Phase 3) ---
    # When enabled, each scan runs change detectors and publishes events
    # (PriceChanged, MarketRegimeChanged, ...) to the in-process bus. Additive:
    # the default subscriber only logs; event-driven recompute arrives with the
    # tier funnel.
    events_enabled: bool = True
    price_change_threshold_pct: float = 1.0  # material intraday move
    flow_burst_premium_usd: float = 250_000.0  # "new large flow" threshold

    # --- Tier funnel (Phase 4) ---
    # Off by default: the funnel is a parallel orchestration path; the simple
    # scan remains the default until the session-aware scheduler cutover.
    tiering_enabled: bool = False
    tier_watchlist_max: int = 50  # Tier 1 -> Tier 2 promotions
    tier_candidates_max: int = 10  # Tier 2 -> Tier 3 promotions
    tier_concurrency: int = 8  # bounded fan-out per tier

    # --- Session-aware scheduler (Phase 5) ---
    # When tiering_enabled, the scheduler process drives each tier at its
    # session-dependent cadence (config/scheduling.yaml) instead of the simple
    # periodic scan. session_tick_seconds is the control-loop granularity.
    session_tick_seconds: int = 10
    scheduling_config_path: str | None = None  # override config/scheduling.yaml

    # --- Short-duration (0DTE / 1-5DTE) module ---
    # Off by default. When enabled, a dedicated fast loop (added in a later phase)
    # drives the module; Phase 1 exposes only read-only views + a context scan.
    # Live trading for this module stays gated behind the global execution guard.
    short_duration_enabled: bool = False
    short_duration_opening_range_minutes: int = 15
    short_duration_max_dte: int = 5
    # Opening-range breakout (0DTE) — adaptive to the session's own volatility.
    # The breakout buffer scales with the opening-range width (a wider, more volatile
    # open needs more room before a break counts) but never falls below a floor.
    # Anti-chase rejects entries already extended too far past the level (poor R:R,
    # you're paying up into the move). Confirmation mode controls how a break is proven.
    orb_min_rel_volume: float = 1.3
    orb_min_break_pct: float = 0.0005          # floor buffer as a fraction of price (0.05%)
    orb_buffer_pct_of_range: float = 0.10      # adaptive buffer = 10% of the OR width
    orb_max_extension_pct_of_range: float = 1.0  # reject if extended > 1.0x OR width past the level
    orb_confirmation_mode: str = "close"       # "close" | "immediate" | "retest"
    orb_retest_band_pct_of_range: float = 0.25  # retest = pulled back within 25% of OR width of level
    orb_require_vwap_alignment: bool = True
    # VWAP-trend continuation (0DTE) — quality-graded. Instead of a hard "never
    # closed on the wrong side of VWAP" gate, the continuation is graded on six
    # sub-scores (continuation/structure/vwap-hold/pullback/volume/controlled-reclaim)
    # and must clear a minimum composite. A brief, cleanly reclaimed VWAP loss is
    # allowed via the controlled-reclaim sub-score; a whipsaw still fails.
    vwap_min_abs_slope_pct: float = 0.0002     # per-bar close slope (fraction of price)
    vwap_lookback_bars: int = 20
    vwap_min_quality: float = 0.45             # minimum composite quality to fire
    # Directional-thesis reversal-risk flag (informational — never gates or scores).
    # A day that moves hard AGAINST the trade, price sitting close to the invalidation
    # level, or a fresh news catalyst are each a reversal-risk factor; two or more
    # rate it "high". Purely to prompt a human sanity-check, e.g. a bearish call on a
    # big green day.
    thesis_reversal_counter_move_pct: float = 2.0   # counter-trend day of this % (or more)
    thesis_reversal_near_invalidation_pct: float = 3.0  # price within this % of invalidation
    thesis_reversal_news_min_score: float = 0.55    # a news catalyst this material counts
    # Structural guardrails (informational): a daily-trend/swing thesis wants at least
    # this many DTE to work; expressing it in a shorter expiry is a horizon mismatch.
    # And an earnings report before the expiry turns a continuation trade into an
    # event binary (IV-crush + gap). Both are surfaced, never auto-gated.
    thesis_swing_min_dte: int = 10
    # Intraday volume profile (time-of-day relative volume). When enabled, relvol
    # uses a historical per-minute median cumulative-volume baseline instead of the
    # flat proration. A thin/absent profile degrades to a LABELLED estimate (or
    # unavailable) — never silently equivalent-quality.
    short_duration_use_volume_profile: bool = True
    short_duration_volume_profile_sessions: int = 20        # lookback (completed sessions)
    short_duration_volume_profile_min_sessions: int = 10    # minimum usable before "estimated"
    short_duration_volume_profile_use_median: bool = True   # median vs mean baseline
    short_duration_volume_profile_cache_minutes: int = 360  # profile is stable within a day
    short_duration_volume_profile_allow_fallback: bool = True
    # Score thresholds (normalized [0,1]) that classify a fresh detection's state.
    short_duration_watchlist_score: float = 0.5
    short_duration_arm_score: float = 0.7
    # Scoring model — weights are configurable + versioned. Every candidate records
    # the model + risk-policy version it was scored under (Phase 2). Weights per
    # model MUST sum to 100. 0DTE v2 rebalance: more weight on price structure and
    # contract liquidity, less on raw flow. 1-5DTE is unchanged.
    scoring_model_version: str = "sd-scoring-2026.07-v2"
    risk_policy_version: str = "sd-risk-2026.07-v1"
    scoring_0dte_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "price_structure": 22, "market_alignment": 15, "relvol_momentum": 15,
            "flow_quality": 10, "contract_liquidity": 18, "volatility": 10,
            "catalyst_news": 5, "risk_reward": 5,
        }
    )
    scoring_1_5dte_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "daily_trend": 20, "catalyst_news": 15, "multi_session_flow": 15,
            "market_alignment": 10, "volatility": 10, "contract_liquidity": 10,
            "technical_entry": 10, "risk_reward": 10,
        }
    )
    # Risk controls (Phase 4). Per-DTE per-trade risk %, tighter than the core
    # scanner; the absolute $ cap (max_defined_risk_per_trade_usd) still applies.
    short_duration_0dte_risk_pct: float = 0.03  # 2-3% baseline
    short_duration_1_5dte_risk_pct: float = 0.05  # 3-5% baseline
    # PAPER VERIFICATION MODE. When true, the short-duration per-trade risk cap is
    # lifted and sizing is forced to 1 contract, so every detected setup becomes an
    # expressible, comparable paper trade instead of rejecting as risk_unmanageable.
    # It NEVER affects live execution (the ExecutionGuard double-gate is separate);
    # it only changes contract sizing for research/paper. Turn OFF for real sizing.
    short_duration_paper_unconstrained: bool = False
    short_duration_max_concurrent: int = 2
    short_duration_daily_loss_pct: float = 0.05  # halt new trades past -5% on the day
    short_duration_consecutive_loss_halt: int = 2  # stop after N straight losses
    short_duration_no_entry_first_minutes: int = 5  # skip the opening scramble
    short_duration_0dte_cutoff_et: str = "15:00"  # no new 0DTE entries after 3pm ET
    # Structure-aware exit plan (Phase 3). 0DTE is managed off structure + the clock:
    # flatten well before the close (no settlement/pin risk), and cut on a momentum
    # stop after N consecutive 1-min closes against the structural level.
    short_duration_0dte_flatten_et: str = "15:45"  # force flat by here (0DTE, even at a loss)
    short_duration_momentum_stop_bars: int = 2     # consecutive 1-min closes against structure
    short_duration_pt1_scale_pct: float = 0.5      # take partial (scale) at PT1
    short_duration_1_5dte_time_stop_dte: int = 1   # close/roll a 1-5DTE by this DTE
    # Data-freshness budgets (seconds), tighter for trade-ready 0DTE candidates.
    # Broad screening tolerates stale data; armed/open 0DTE needs seconds-fresh quotes.
    freshness_broad_underlying_s: int = 120
    freshness_broad_option_s: int = 120
    freshness_watchlist_underlying_s: int = 30
    freshness_watchlist_option_s: int = 30
    freshness_armed_underlying_s: int = 8    # 0DTE armed/triggered underlying (5-10)
    freshness_armed_option_s: int = 12       # selected option quote (5-15)
    freshness_armed_internals_s: int = 30    # market internals for a trade-ready name
    freshness_armed_account_s: int = 60      # broker/account state
    freshness_open_underlying_s: int = 5     # open 0DTE position
    freshness_open_option_s: int = 10
    # Dedicated fast-loop cadences (seconds). Only runs when SHORT_DURATION_ENABLED
    # and during RTH. Position monitoring is the most frequent (capital at risk).
    short_duration_loop_tick_seconds: int = 5
    short_duration_monitor_seconds: int = 15
    short_duration_scan_0dte_seconds: int = 300  # 5 min
    short_duration_scan_1_5dte_seconds: int = 900  # 15 min

    # --- Account / risk policy ---
    # Defaults are the "aggressive but defined-risk" profile: 5%/trade, 15%
    # account. This aligns the % cap with the $100 absolute per-trade cap and
    # makes the mega-cap universe tradeable with defined-risk spreads. A $2k
    # account cannot size these spreads at a 2% ($40) cap. Tighten via env for a
    # more conservative stance (and pair it with a lower-priced universe).
    account_equity_usd: float = 2_000.0
    max_account_risk_pct: float = 0.15
    max_trade_risk_pct: float = 0.05
    max_concurrent_positions: int = 4
    max_defined_risk_per_trade_usd: float = 100.0
    max_contracts_per_trade: int = 20  # concentration / fill-risk cap

    # --- Database ---
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "atb"
    postgres_user: str = "atb"
    postgres_password: str = "change_me"
    database_url: str | None = None
    # Turso / libSQL (durable cloud SQLite). When set, it takes precedence.
    turso_database_url: str | None = None  # libsql://<db>.turso.io
    turso_auth_token: str | None = None

    # --- Redis ---
    redis_url: str = "redis://localhost:6379/0"

    # --- Provider efficiency (Phase 2) ---
    # Response cache: short TTLs on volatile data (quotes/chains), long on static
    # (fundamentals/IV history). Backend defaults to in-process memory; set
    # CACHE_BACKEND=redis to use Redis (falls back to memory if unreachable).
    cache_enabled: bool = True
    cache_backend: str = "memory"  # memory | redis
    cache_ttl_scale: float = 1.0  # global multiplier on all TTLs
    # Rate limiting: per-provider token bucket honoring documented req/min.
    rate_limit_enabled: bool = True
    rate_limit_default_rpm: int = 120  # fallback for providers without an entry
    # Request budget: opt-in hard daily cap per provider (safety kill-switch).
    api_budget_enabled: bool = False
    api_daily_budget: int = 10_000  # per provider, when api_budget_enabled

    # --- Alerts ---
    alerts_enabled: bool = False
    alerts_channel: str = "console"  # console | slack | noop
    alerts_min_score: float = 0.6  # only alert on candidates at/above this score
    slack_webhook_url: str | None = None

    # --- Provider routing ---
    provider_market_data: ProviderName = ProviderName.MOCK
    provider_options_flow: ProviderName = ProviderName.MOCK
    provider_options_chain: ProviderName = ProviderName.MOCK
    provider_fundamentals: ProviderName = ProviderName.MOCK
    provider_calendar: ProviderName = ProviderName.MOCK
    provider_brokerage: ProviderName = ProviderName.MOCK
    # Optional. If unset, IV rank falls back to a realized-vol proxy from real
    # price history rather than an opaque provider field.
    provider_iv_history: ProviderName | None = ProviderName.MOCK
    # Short-duration data capabilities (intraday bars, news, macro calendar).
    provider_intraday: ProviderName = ProviderName.MOCK
    provider_news: ProviderName = ProviderName.MOCK
    provider_econ_calendar: ProviderName = ProviderName.MOCK
    # Real market internals: "mock", or anything else -> the FMP+UW composite feed
    # (sector breadth + options-flow tide). Set PROVIDER_MARKET_INTERNALS=composite.
    provider_market_internals: str = "mock"
    # Account-state source that sizing reads: "paper" (default; the simulated book:
    # configured base + realized paper P&L, minus open defined-risk) or "fallback"
    # (the configured equity as a bare constant). Both are UNVERIFIED — a live broker
    # feed lands later and is the only verified source. Live execution stays gated.
    provider_account_state: str = "paper"

    # --- Provider credentials ---
    fmp_api_key: str | None = None
    fmp_base_url: str = "https://financialmodelingprep.com"
    unusual_whales_api_key: str | None = None
    unusual_whales_base_url: str = "https://api.unusualwhales.com"
    robinhood_username: str | None = None
    robinhood_password: str | None = None
    robinhood_mfa_secret: str | None = None
    benzinga_api_key: str | None = None
    benzinga_base_url: str = "https://api.benzinga.com"

    @field_validator("provider_iv_history", mode="before")
    @classmethod
    def _empty_to_none(cls, v: object) -> object:
        # An empty env value means "no IV-history feed" -> None (HV proxy).
        if isinstance(v, str) and v.strip() == "":
            return None
        return v

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sqlalchemy_url(self) -> str:
        if self.database_url:
            return self.database_url
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def automation_armed(self) -> bool:
        """Automation requires BOTH the kill-switch and the explicit mode.

        This double-gate is deliberate: flipping a single flag can never be
        enough to allow live automated order placement.
        """
        return self.automation_enabled and self.trading_mode == TradingMode.AUTOMATION

    @model_validator(mode="after")
    def _validate_risk(self) -> Settings:
        if not (0 < self.max_trade_risk_pct <= self.max_account_risk_pct <= 1):
            raise ValueError(
                "Require 0 < MAX_TRADE_RISK_PCT <= MAX_ACCOUNT_RISK_PCT <= 1"
            )
        if self.account_equity_usd <= 0:
            raise ValueError("ACCOUNT_EQUITY_USD must be positive")
        if self.max_concurrent_positions < 1:
            raise ValueError("MAX_CONCURRENT_POSITIONS must be >= 1")
        if self.scan_interval_minutes < 1:
            raise ValueError("SCAN_INTERVAL_MINUTES must be >= 1")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
