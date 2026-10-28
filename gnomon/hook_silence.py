"""Hook-process log silencer (checklist g48d). Pure stdlib, no app imports.

A harness hook (``python -m gnomon.harness.<hook>``) has a stderr that is a VERDICT CHANNEL (deny_guard appends it to
the block message the model reads). Nothing a hook imports may reach it. The silencing lives HERE, called at the top of each
hook's ``main`` -- never as a NullHandler on a shared module's logger, which would silence the same warnings in long-lived
processes (web, hub, launcher) that have no root handler.
"""

import logging


def silence_logging() -> None:
    """Idempotent: a root NullHandler (so a handler 'exists') and ``logging.lastResort = None`` (so Python's stderr
    fallback can never fire). Records still propagate to any handler a caller adds later."""
    root = logging.getLogger()
    if not any(type(h) is logging.NullHandler for h in root.handlers):
        root.addHandler(logging.NullHandler())
    logging.lastResort = None
