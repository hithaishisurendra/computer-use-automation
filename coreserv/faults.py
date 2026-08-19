"""Server-side fault flags. Global, in-memory, not URL parameters.

Toggled via the control endpoints in main.py (POST/GET /_faults, POST /_faults/reset).
This lets the same recorded artifact run with the same inputs and produce a
different outcome because the world changed underneath it, not because the
run itself was edited.
"""

FAULT_NAMES = [
    "member_not_found",
    "restricted_member",
    "maintenance_interstitial",
    "slow_response",
    "session_expired",
    "validation_error",
    "server_error",
]

_flags = {name: False for name in FAULT_NAMES}


def get_all() -> dict:
    return dict(_flags)


def is_enabled(name: str) -> bool:
    return _flags.get(name, False)


def set_flag(name: str, enabled: bool) -> bool:
    if name not in _flags:
        return False
    _flags[name] = enabled
    return True


def reset_all() -> None:
    for name in _flags:
        _flags[name] = False
