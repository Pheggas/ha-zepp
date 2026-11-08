"""Device naming.

Two fallbacks, in order: the ``displayName`` the cloud returns ("Amazfit T-Rex 3
Pro 48mm/44mm"), then a ``deviceSource`` lookup.

The previous version downloaded a device list from GitHub during setup, which
made start-up depend on that repository being reachable and put a network call
in the middle of an otherwise local step. The static table covers the models
that are actually in use; anything newer arrives with a ``displayName`` anyway.
"""
from __future__ import annotations

# deviceSource code -> model name. Confirmed against the ZeppOS device list.
DEVICE_SOURCES: dict[int, str] = {
    10551553: "Amazfit T-Rex 3 Pro",
    6553856: "Amazfit T-Rex Ultra",
    6553857: "Amazfit T-Rex Ultra",
    7930112: "Amazfit GTR 4",
    7930113: "Amazfit GTR 4",
    8192257: "Amazfit Cheetah",
    8257793: "Amazfit Cheetah Square",
    8323328: "Amazfit Active",
    8323329: "Amazfit Active",
    8388864: "Amazfit Active Edge",
    8388865: "Amazfit Active Edge",
    8454400: "Amazfit Bip 5",
    8454401: "Amazfit Bip 5",
    8519936: "Amazfit Balance",
    8519937: "Amazfit Balance",
    8519939: "Amazfit Balance",
    8716544: "Amazfit T-Rex 3",
    8716545: "Amazfit T-Rex 3",
    8716547: "Amazfit T-Rex 3",
    8782081: "Amazfit Bip 5 Unity",
    8913155: "Amazfit Active 2",
    9568512: "Amazfit Balance 2",
    9765120: "Amazfit Bip 6",
    10158337: "Amazfit Bip 6",
    10879233: "Amazfit T-Rex Ultra 2",
    11141377: "Amazfit Balance 3",
}


def resolve_device_name(device_source: object, display_name: object = None) -> str:
    """Best available name for a watch."""
    if isinstance(display_name, str) and display_name.strip():
        return display_name.strip()
    try:
        code = int(device_source)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        code = None
    if code is not None and code in DEVICE_SOURCES:
        return DEVICE_SOURCES[code]
    if isinstance(device_source, str) and device_source.strip() and not device_source.isdigit():
        return device_source.strip()
    return "Amazfit Watch"
