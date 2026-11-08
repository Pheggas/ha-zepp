"""Run every test module and report one exit code."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

MODULES = ["test_decoders.py", "test_history.py", "test_imports.py"]


def main() -> int:
    failed = 0
    for module in MODULES:
        print(f"\n=== {module} ===")
        result = subprocess.run([sys.executable, str(HERE / module)], cwd=HERE.parent)
        if result.returncode:
            failed += 1
    print("\nall suites passed" if not failed else f"\n{failed} suite(s) failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
