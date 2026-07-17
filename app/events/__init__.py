"""Event-driven core: typed events, an async pub/sub bus, and change detectors.

The platform reacts to meaningful change (price moves, flow bursts, regime
shifts, position/order/risk transitions) rather than only recomputing on a
timer. Detectors turn raw data into events; the bus fans them out to
subscribers. In-process by default; a Redis-backed transport can slot in later
behind the same interface.
"""
