"""P2-T4 离线外部题库语料导入 CLI（DiagDBench / D-Bot / BIRD-CRITIC-1 / BenchBase）。

用法：
    python scripts/import_external_corpus.py --source diagdb --input path/to/diagdb.jsonl --out runs/suite-v2-ext.json

硬约束：
- 绝不修改冻结的 benchmarks/w10/suite-v1.json；
- 仅在同引擎同场景存在本地冻结故障清单时挂载，未映射条目记入 external_unmapped；
- 出口经过 assert_no_oracle_leak 与 RedactionGate 复检。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from benchmarks.external.convert import load_external_jsonl  # noqa: E402
from benchmarks.external.suite_v2 import build_suite_v2  # noqa: E402
from core.benchmark.oracle import assert_no_oracle_leak  # noqa: E402
from core.redaction.engine import DataRedactionEngine  # noqa: E402
from core.redaction.gate import RedactionGate  # noqa: E402
from core.redaction.models import RedactionStatus  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="离线导入外部数据库诊断题库并构建 suite-v2")
    parser.add_argument(
        "--source",
        required=True,
        choices=("diagdb", "dbot", "bird_critic", "benchbase"),
        help="外部基准源名称",
    )
    parser.add_argument("--input", required=True, help="本地 JSONL 语料文件路径")
    parser.add_argument("--out", required=True, help="输出扩张套件 JSON 路径")
    args = parser.parse_args(argv)

    cases = load_external_jsonl(Path(args.input), source=args.source)
    suite = build_suite_v2(external_cases=cases)
    serialized = json.dumps(suite, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    # 校验零 Oracle 泄漏与出口脱敏
    assert_no_oracle_leak(
        [str(c.get("case_id", "")) for c in suite.get("cases", [])]
        + [str(c.get("scenario", "")) for c in suite.get("cases", [])]
    )
    gate = RedactionGate(DataRedactionEngine())
    scan = gate.for_document(serialized, channel="EXPORT")
    if scan.status not in (RedactionStatus.CLEAN, RedactionStatus.REDACTED):
        raise SystemExit(f"出口脱敏闸门拦截：{scan.status.value}")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(serialized, encoding="utf-8")
    prov = suite.get("provenance", {})
    print(
        json.dumps(
            {
                "source": args.source,
                "loaded": len(cases),
                "attached": len(prov.get("external_attached", [])),
                "unmapped": len(prov.get("external_unmapped", [])),
                "out": str(out_path),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
