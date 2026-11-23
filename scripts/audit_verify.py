"""审计链巡检 CLI（人工核对用，P0-13/T6）。

用法：
    python scripts/audit_verify.py                    # 巡检测试库中所有任务的链
    python scripts/audit_verify.py --task-id task_xxx  # 只校验一个任务
    python scripts/audit_verify.py --limit 50 --json   # 机器可读输出

退出码：0 = 全部通过；1 = 至少一条链断裂（或存在无法校链的调用参数）。
输出结构与 API `POST /v1/audit/verify` 完全一致，便于人工对照。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.audit.service import AuditService  # noqa: E402
from core.config import Settings  # noqa: E402
from core.errors import DBGuardianError  # noqa: E402
from core.tasks.repository import TaskRepository  # noqa: E402
from database.mysql.connection import MysqlClient  # noqa: E402


def _list_task_ids(client: MysqlClient, limit: int) -> list[str]:
    rows = client.fetch_all(
        "SELECT task_id FROM dg_system.task_events GROUP BY task_id "
        "ORDER BY MAX(created_at) DESC LIMIT %s",
        (limit,),
    )
    return [str(row["task_id"]) for row in rows]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DBGuardian 审计链巡检")
    parser.add_argument("--task-id", default=None, help="只校验指定任务")
    parser.add_argument("--limit", type=int, default=20, help="最多巡检多少个任务")
    parser.add_argument("--json", action="store_true", help="输出 JSON（机器可读）")
    parser.add_argument("--worm-endpoint", default=None, help="可选：S3/MinIO Object Lock 端点")
    parser.add_argument("--worm-bucket", default="dg-audit-worm", help="WORM 归档桶名")
    parser.add_argument("--worm-ref-json", default=None, help="可选：ArchiveRef JSON 文件路径")
    args = parser.parse_args(argv)

    settings = Settings()
    client = MysqlClient(
        settings.control_mysql_dsn(),
        database_id="dg_system",
        read_only=False,
        connect_timeout_s=settings.connect_timeout_s,
        statement_timeout_ms=settings.statement_timeout_ms,
        max_result_rows=settings.max_result_rows,
    )
    repository = TaskRepository(client)
    service = AuditService(repository)

    task_ids = [args.task_id] if args.task_id else _list_task_ids(client, max(1, args.limit))
    results: list[dict[str, object]] = []
    failed = 0
    for task_id in task_ids:
        rows = client.fetch_all(
            "SELECT tenant_id FROM dg_system.tasks WHERE task_id = %s LIMIT 1", (task_id,)
        )
        if not rows:
            results.append(
                {
                    "task_id": task_id,
                    "ok": False,
                    "reason": "TASK_NOT_FOUND",
                    "checked": 0,
                    "broken_at_seq": None,
                    "affected_range": None,
                }
            )
            failed += 1
            continue
        tenant_id = str(rows[0]["tenant_id"])
        try:
            verification = service.verify(tenant_id=tenant_id, task_id=task_id)
        except DBGuardianError as exc:
            results.append(
                {
                    "task_id": task_id,
                    "ok": False,
                    "reason": exc.code.value,
                    "checked": 0,
                    "broken_at_seq": None,
                    "affected_range": None,
                }
            )
            failed += 1
            continue
        payload = verification.to_public_dict()
        payload["task_id"] = task_id
        results.append(payload)
        if not verification.ok:
            failed += 1

    worm_result: dict[str, object] | None = None
    if args.worm_endpoint and args.worm_ref_json:
        import os

        from core.audit.worm import ArchiveRef, S3ObjectLockArchiveSink, S3ObjectLockConfig

        ref_data = json.loads(Path(args.worm_ref_json).read_text(encoding="utf-8"))
        ref = ArchiveRef(
            location=str(ref_data["location"]),
            sequence=int(ref_data["sequence"]),
            count=int(ref_data["count"]),
            root=str(ref_data["root"]),
            previous_root=ref_data.get("previous_root"),
        )
        sink = S3ObjectLockArchiveSink(
            S3ObjectLockConfig(
                endpoint_url=args.worm_endpoint,
                bucket=args.worm_bucket,
                access_key=os.environ.get("DG_WORM_ACCESS_KEY", "minioadmin"),
                secret_key=os.environ.get("DG_WORM_SECRET_KEY", "minioadmin"),
                retention_mode=os.environ.get("DG_WORM_RETENTION_MODE", "COMPLIANCE"),  # type: ignore[arg-type]
            )
        )
        try:
            worm_ok = sink.verify(ref)
        finally:
            sink.close()
        worm_result = {"endpoint": args.worm_endpoint, "bucket": args.worm_bucket, "ok": worm_ok}
        if not worm_ok:
            failed += 1

    if args.json:
        payload_out: dict[str, object] = {"results": results, "failed": failed}
        if worm_result is not None:
            payload_out["worm"] = worm_result
        print(json.dumps(payload_out, ensure_ascii=False, indent=2))
    else:
        print(f"巡检任务数：{len(results)}；断裂：{failed}")
        for item in results:
            status = "OK  " if item["ok"] else "FAIL"
            detail = ""
            if not item["ok"]:
                detail = (
                    f" broken_at_seq={item['broken_at_seq']}"
                    f" affected_range={item['affected_range']} reason={item['reason']}"
                )
            print(f"[{status}] {item['task_id']} checked={item['checked']}{detail}")
        if worm_result is not None:
            print(f"[WORM] {'OK' if worm_result['ok'] else 'FAIL'} {worm_result['endpoint']}/{worm_result['bucket']}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
