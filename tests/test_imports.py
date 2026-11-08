"""Import every module of the integration against the Home Assistant stubs.

This catches the failure mode that a syntax check misses: a name that no longer
exists, a base class that is subscripted wrongly, or a platform module that
imports a helper the package never defines.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import ha_stub  # noqa: E402

ha_stub.install()

MODULES = [
    "custom_components.zepp",
    "custom_components.zepp.api",
    "custom_components.zepp.button",
    "custom_components.zepp.config_flow",
    "custom_components.zepp.const",
    "custom_components.zepp.coordinator",
    "custom_components.zepp.decoders",
    "custom_components.zepp.device_catalog",
    "custom_components.zepp.diagnostics",
    "custom_components.zepp.entities",
    "custom_components.zepp.history",
    "custom_components.zepp.models",
    "custom_components.zepp.sensor",
    "custom_components.zepp.services",
    "custom_components.zepp.stats",
]


def main() -> int:
    failures = 0
    for name in MODULES:
        try:
            importlib.import_module(name)
            print(f"ok   {name}")
        except Exception as err:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}: {type(err).__name__}: {err}")
    print(f"\n{len(MODULES) - failures}/{len(MODULES)} imported")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
