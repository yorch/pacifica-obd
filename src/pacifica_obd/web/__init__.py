"""Local web UI for pacifica-obd (optional ``[web]`` extra).

Nothing here is imported by the CLI/TUI at startup: importing this package must
never require FastAPI, so the lazy imports live in :mod:`pacifica_obd.web.app`
and :mod:`pacifica_obd.web.__main__`. The read-only operation layer
(:mod:`pacifica_obd.web.service`) and the bus worker (:mod:`pacifica_obd.web.bus`)
are dependency-free and importable on their own.
"""

__all__ = []
