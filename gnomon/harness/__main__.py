"""``python -m gnomon.harness <session_dir>`` -> the launcher.

The terminal template runs exactly this.
"""

from __future__ import annotations

from .launcher import main

if __name__ == "__main__":
    raise SystemExit(main())
