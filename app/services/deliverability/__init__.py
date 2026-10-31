"""Deliverability: what a sending domain looks like from outside, and whether it is healthy.

Nothing in this package may raise a cap, change a send mode or send anything. It reports, and in
a later slice it may **pause** a mailbox. That asymmetry is deliberate (ADR-034): a signal that
can only restrict cannot be the cause of an outage it then reports.
"""
