"""Operating modes: research, paper, human-approval, and gated automation.

The execution guard is the single chokepoint through which any live order must
pass. It is deliberately conservative: live execution is denied unless BOTH the
automation kill-switch and the automation mode are set, and every attempt is
logged for traceability.
"""
