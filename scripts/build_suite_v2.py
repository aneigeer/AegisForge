"""生成 `benchmarks/w10/suite-v2.json`（P2-T1B）。

用法（仓库根目录）：

    .\\.venv\\Scripts\\python.exe scripts/build_suite_v2.py
    .\\.venv\\Scripts\\python.exe scripts/build_suite_v2.py --repetitions 8

**不改写** `suite_manifest.json`（suite-v1 冻结件）；脚本只新增/覆盖 `suite-v2.json`。
外部语料（DiagDBench / D-Bot / BIRD-CRITIC / BenchBase）当前不可获取时，
`provenance.external_attached` 为空数组，`external_unmapped` 如实留空——
不生成任何"看起来有外部数据"的条目。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # 允许从任意 cwd 直接运行
    sys.path.insert(0, str(ROOT))

from benchmarks.external.suite_v2 import SUITE_V1_PATH, write_suite_v2  # noqa: E402

DEFAULT_TARGET = ROOT / "benchmarks" / "w10" / "suite-v2.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 DBGuardian suite-v2（不触碰 suite-v1）")
    parser.add_argument("--repetitions", type=int, default=6, help="每个场景展开的 block 数")
    parser.add_argument("--out", default=str(DEFAULT_TARGET), help="输出路径")
    args = parser.parse_args(argv)

    target = write_suite_v2(Path(args.out), repetitions=args.repetitions)
    print(f"suite-v2 已生成：{target}")
    print(f"suite-v1 未改动：{SUITE_V1_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
