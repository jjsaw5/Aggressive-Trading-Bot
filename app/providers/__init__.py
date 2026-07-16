"""Provider abstraction layer.

Every external data/broker integration implements one of the capability
interfaces in `base.py`. The rest of the system depends only on those
interfaces, never on a concrete provider — so FMP can be swapped for Polygon,
or the mock provider used in tests, with zero call-site changes.
"""
