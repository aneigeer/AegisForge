"""发布件重出脚本（W12 发布件同步卡：C-W12-4 / C-W12-5 + D-06 迁移说明同步）。

**为什么需要脚本**：`docs/w12/known-limitations.{json,md}`、`openapi.json`、
`alpha-acceptance-report.md`、`release-notes.md`、`claim-scan-report.md` 以及 W11 的
`benchmarks/w11/rc_manifest.json` / `docs/w11/rc-freeze.md` 都是**函数产物**，
不得手工编辑（改一次就会与生成器漂移，且 `items_digest` / `decision_digest` 无法对账）。
本脚本是这些产物的**唯一**重出入口。

| 产物 | 生成器 |
|---|---|
| `benchmarks/w11/rc_manifest.json` | ``core.assurance.rc.collect_rc_manifest()`` |
| `docs/w11/rc-freeze.md` | ``core.assurance.rc.build_rc_freeze()`` |
| `docs/w12/openapi.json` | ``core.release.docs.export_openapi_31()`` |
| `docs/w12/known-limitations.{json,md}` | ``core.release.docs.build_full_known_limitations()`` + ``write_known_limitations()`` |
| `docs/w12/alpha-acceptance-report.md`、`docs/w12/release-notes.md` | ``core.assurance.publish.publish_decision()`` → ``core.release.publish.derive_release_type()`` → ``write_release_documents()`` |
| `docs/w12/claim-scan-report.md` | ``core.release.claims.regenerate_claim_scan_report()``（两遍，最后执行） |

**不降级**：验收段（AC-01…AC-09）必须实跑控制库与 LAB；控制库/装配不可用时脚本
**显式失败**，不写「未实测却标 PASSED」的发布件（INV-06）。

用法：
    .\\.venv\\Scripts\\python.exe scripts\\w12_release.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:  # 允许直接以脚本方式运行
    sys.path.insert(0, str(_ROOT))

from adapters.lab_assembly import build_lab_registry  # noqa: E402
from core.assurance.publish import (  # noqa: E402
    ENVIRONMENT_BLOCK_IDS,
    build_limitations,
    publish_decision,
)
from core.assurance.rc import (  # noqa: E402
    DEFAULT_REPORT_INDEX,
    build_rc_freeze,
    collect_rc_manifest,
    limitations_digest,
)
from core.config import Settings  # noqa: E402
from core.release.claims import regenerate_claim_scan_report  # noqa: E402
from core.release.deploy import collect_deploy_package  # noqa: E402
from core.release.docs import (  # noqa: E402
    build_full_known_limitations,
    collect_doc_inventory,
    export_openapi_31,
    write_known_limitations,
)
from core.release.functional import (  # noqa: E402
    LAB_ORDER_ID,
    run_functional_acceptance,
)
from core.release.publish import (  # noqa: E402
    build_alpha_acceptance_report,
    derive_release_type,
    write_release_documents,
)
from core.release.security import run_security_acceptance  # noqa: E402
from core.tasks.repository import TaskRepository  # noqa: E402
from database.mysql.connection import MysqlClient  # noqa: E402

_RC_MANIFEST = "benchmarks/w11/rc_manifest.json"
_RC_FREEZE = "docs/w11/rc-freeze.md"


def _client(settings: Settings, dsn: str, database_id: str) -> MysqlClient:
    return MysqlClient(
        dsn,
        database_id=database_id,
        read_only=False,
        connect_timeout_s=settings.connect_timeout_s,
        statement_timeout_ms=settings.statement_timeout_ms,
        max_result_rows=settings.max_result_rows,
    )


def _pg_probe(settings: Any):
    """PG 只读探针（P2-T1）：连通性 + `pg_stat_statements` 可查才算真可用。"""

    def probe() -> bool:
        from adapters.postgres_mcp.tools import execute_tool  # noqa: PLC0415
        from database.postgres.connection import PgClient  # noqa: PLC0415

        if settings.pg_ro_dsn is None:
            return False
        client = PgClient(
            settings.pg_ro_dsn.get_secret_value(),
            database_id=settings.pg_database_id,
            read_only=True,
            connect_timeout_s=settings.connect_timeout_s,
            statement_timeout_ms=settings.statement_timeout_ms,
            max_result_rows=settings.max_result_rows,
        )
        if not client.fetch_all("SELECT 1 AS ok"):
            return False
        stats = execute_tool(
            "get_query_stats",
            {"limit": 5},
            client=client,
            database_id=settings.pg_database_id,
            db_version="16.4",
        )
        return bool((stats.data or {}).get("available"))

    return probe


def _decision():  # noqa: ANN202 - 返回 PublishDecision
    """W11 发布决定（与 `docs/w11/limitations.json` **同一次调用口径**）。

    逐案实测结果未以机器可读件落盘，故三份结果按空序列 + 实测环境阻塞调用：
    完备性不足 → `TECH_PREVIEW_ONLY` / `reason=ENV_BLOCK_UNRESOLVED`。
    **不伪造通过**：宁可让完备性缺口决定档位，也不把未落盘的结论当成已实测。
    """
    return publish_decision(
        negative=(),
        concurrency=(),
        faults=(),
        environment_blocks=ENVIRONMENT_BLOCK_IDS,
    )


def _write_w11_rc(root: Path) -> None:
    """重出 W11 RC 冻结件（`docs/migrations.md` 覆盖度变化会改变 `docs_migrations_lag`）。"""
    manifest = collect_rc_manifest(repo_root=root)
    decision = _decision()
    limitations = build_limitations(decision)
    (root / _RC_MANIFEST).write_text(
        json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    freeze = build_rc_freeze(
        manifest=manifest,
        decision=decision,
        limitations=limitations,
        reports=DEFAULT_REPORT_INDEX,
    )
    assert limitations_digest(limitations) in freeze
    (root / _RC_FREEZE).write_text(freeze, encoding="utf-8")
    print(f"[w11] rc_manifest.json / rc-freeze.md 已重出（docs_migrations_lag={manifest.docs_migrations_lag}）")


def _write_w12_docs(root: Path) -> None:
    """重出 W12 合同件与限制清单（不依赖数据库）。"""
    contract = export_openapi_31(root)
    print(f"[w12] openapi.json 已重出（{contract.openapi_version}，{contract.path_count} paths）")

    decision = _decision()
    limitations = build_full_known_limitations(root, decision=decision)
    write_known_limitations(root, limitations)
    auto = sum(1 for item in limitations.items if item.origin.value == "AUTO")
    manual = len(limitations.items) - auto
    print(
        f"[w12] known-limitations 已重出（{len(limitations.items)} 项 = {auto} AUTO + {manual} MANUAL，"
        f"items_digest={limitations.items_digest[:12]}…）"
    )


def _write_w12_release(root: Path, settings: Settings) -> None:
    """实跑 AC-01…AC-09 后重出验收报告与发布说明（消费真实 publish_decision）。"""
    # W11 真实负面集端口与跨租户对象：复用 W11 冻结接缝，不重写其逻辑。
    from tests.integration.test_w11_security_negative import (  # noqa: PLC0415
        _NegativePorts,
        provision_cross_tenant_objects,
    )
    from workers.stack import build_worker_stack  # noqa: PLC0415

    control = _client(settings, settings.control_mysql_dsn(), "dg_system")
    control.fetch_all("SELECT 1 AS ok")

    repository = TaskRepository(control)
    environment_row = repository.get_environment(
        settings.default_environment_id, tenant_id=settings.default_tenant_id
    )
    if environment_row is None:
        raise SystemExit("默认 MySQL 环境不存在：请先执行 scripts/bootstrap_mysql.py")
    registry = build_lab_registry(None, environment_row, "mysql")
    stack = build_worker_stack(settings)
    ports = _NegativePorts(
        client=control,
        settings=settings,
        objects=provision_cross_tenant_objects(control),
    )

    rw = _client(settings, settings.rw_dsn, settings.database_id)

    def _row_probe() -> str | None:
        rows = rw.fetch_all(
            "SELECT note FROM dg_lab.seed_orders WHERE order_id = %s LIMIT 1",
            (LAB_ORDER_ID,),
        )
        return None if not rows else rows[0].get("note")

    functional = run_functional_acceptance(
        control_client=control, repair_stack=stack, pg_probe=_pg_probe(settings)
    )
    security = run_security_acceptance(
        registry=registry,
        control_client=control,
        negative_ports=ports,
        row_probe=_row_probe,
    )
    deploy = collect_deploy_package(root)
    docs = collect_doc_inventory(root)
    decision = _decision()

    report = build_alpha_acceptance_report(
        functional=functional,
        security=security,
        deploy=deploy,
        docs=docs,
        decision=decision,
        root=root,
    )
    gate = derive_release_type(decision=decision)
    write_release_documents(root, report=report, gate=gate)
    print(
        f"[w12] 发布件已重出：release_type={gate.release_type.value} "
        f"source={gate.source} decision_digest={(gate.decision_digest or '')[:12]}… "
        f"登记项={gate.registered_issues or '无'}"
    )


def main() -> int:
    root = _ROOT
    settings = Settings()
    _write_w11_rc(root)
    _write_w12_docs(root)
    _write_w12_release(root, settings)
    # 表述扫描必须最后跑：它自身也在被扫描范围内（两遍写）。
    result = regenerate_claim_scan_report(root)
    print(
        f"[w12] claim-scan-report.md 已重出：ok={result.ok} "
        f"violations={len(result.violations)} exempted={result.exempted_lines}"
    )
    if not result.ok:
        for item in result.violations:
            print(f"  VIOLATION: {item}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
