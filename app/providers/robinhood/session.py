"""Robinhood session management.

robin_stocks is synchronous (uses `requests`), so every call is dispatched to a
thread to preserve the async provider interface. Login is lazy and performed
once; MFA is handled headlessly with a pyotp TOTP derived from
`ROBINHOOD_MFA_SECRET` (interactive stdin challenges are unusable in a service).

The library and pyotp are optional imports — a clear error is raised if the
`[robinhood]` extra is not installed, rather than failing obscurely.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from app.config import settings
from app.logging_config import get_logger

log = get_logger(__name__)


class RobinhoodDependencyError(RuntimeError):
    pass


class RobinhoodAuthError(RuntimeError):
    pass


def _import_robinhood():
    try:
        import robin_stocks.robinhood as rh  # type: ignore
    except ImportError as exc:  # pragma: no cover - env-dependent
        raise RobinhoodDependencyError(
            "robin_stocks is not installed. Install the extra: "
            'pip install -e ".[robinhood]"'
        ) from exc
    return rh


def _totp_now() -> str | None:
    if not settings.robinhood_mfa_secret:
        return None
    try:
        import pyotp  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise RobinhoodDependencyError(
            "pyotp is required for Robinhood MFA. Install: pip install -e \".[robinhood]\""
        ) from exc
    return pyotp.TOTP(settings.robinhood_mfa_secret).now()


class RobinhoodSession:
    """Lazy, single-login session with thread-dispatched calls."""

    def __init__(self) -> None:
        self._rh = None
        self._logged_in = False
        self._lock = asyncio.Lock()

    async def _ensure_login(self) -> None:
        if self._logged_in:
            return
        async with self._lock:
            if self._logged_in:
                return
            if not (settings.robinhood_username and settings.robinhood_password):
                raise RobinhoodAuthError(
                    "ROBINHOOD_USERNAME / ROBINHOOD_PASSWORD are not set."
                )
            rh = _import_robinhood()

            def _login() -> None:
                rh.login(
                    username=settings.robinhood_username,
                    password=settings.robinhood_password,
                    mfa_code=_totp_now(),
                    store_session=True,
                )

            await asyncio.to_thread(_login)
            self._rh = rh
            self._logged_in = True
            log.info("robinhood_login_ok")

    async def call(self, fn_path: str, *args: Any, **kwargs: Any) -> Any:
        """Call `robin_stocks.robinhood.<fn_path>(*args, **kwargs)` in a thread.

        `fn_path` is a dotted path within the module, e.g. "stocks.get_quotes"
        or "options.get_chains".
        """
        await self._ensure_login()
        rh = self._rh
        target: Callable = rh  # type: ignore[assignment]
        for part in fn_path.split("."):
            target = getattr(target, part)
        return await asyncio.to_thread(target, *args, **kwargs)
