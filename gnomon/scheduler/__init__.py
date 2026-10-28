"""The Gnomon Scheduler (spec 14; stage 1, sub-stage 1a: confirmed own-session timed injects).

This package is imported LAZILY (inside functions) by the hub and the web job: nothing here runs at import, and the
hub / hook closure never imports it at top level (g47b/g67). Behind ``scheduler`` (default OFF).
"""
