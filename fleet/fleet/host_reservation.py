"""host-reservation@1 adapter: an effective hostgate reservation persisted before any start.

The authority always comes from ``admin.local_authority()``; nothing here reads an
environment variable or argument to choose it.
"""
from .model import Fault


def authority():
    """Effective hostgate configuration, or backend_unavailable without creating state."""
    try:
        from hostgate import admin
        config = admin.load_config(admin.local_authority())
    except Exception:
        raise Fault("backend_unavailable") from None
    if config.node.mode != "effective":
        raise Fault("backend_unavailable")
    return config
