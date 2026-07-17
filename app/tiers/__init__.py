"""The four-tier market-monitoring funnel.

Confidence and capital rise as symbols move down the funnel, so cadence and API
priority rise with them:

    Tier 1  Broad universe   500-1500 symbols  cheap eval, no chains   BROAD
    Tier 2  Active watchlist   20-50 symbols   flow + trend + IV       WATCHLIST
    Tier 3  Trade candidates    3-10 symbols   full chain + greeks     CANDIDATES
    Tier 4  Open positions      held           mark + risk + exits     POSITIONS

Each tier reuses the existing analyzers/engine at a different depth, runs at its
own request priority (Phase 2 rate limiter), persists membership (Turso), and
publishes events (Phase 3) on promotion and position risk.
"""
