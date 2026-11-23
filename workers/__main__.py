"""`python -m workers` 入口（等价于 `python -m workers.cli`）。"""

from __future__ import annotations

import sys

from workers.cli import main

if __name__ == "__main__":
    sys.exit(main())
