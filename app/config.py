"""Application configuration via Pydantic settings.

All configuration is sourced from environment variables (see `.env.example`).
Nothing in the codebase should read `os.environ` directly — import `settings`.
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache

from pydantic import computed_field, field_validator, model_validator
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

    # --- Provider credentials ---
    fmp_api_key: str | None = None
    fmp_base_url: str = "https://financialmodelingprep.com"
    unusual_whales_api_key: str | None = None
    unusual_whales_base_url: str = "https://api.unusualwhales.com"
    robinhood_username: str | None = None
    robinhood_password: str | None = None
    robinhood_mfa_secret: str | None = None

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
