r"""修复历史事件的哈希链（仅用于 002 迁移前已存在事件的数据库）。

运行时 event_hash 使用规范化 JSON + UTC ISO 时间；002 迁移的 SQL 回填无法完全
复现该算法，因此对旧库执行一次本脚本，按运行时代码重算 seq 对应的
event_hash 与 previous_event_hash。

用法：
    .\.venv\Scripts\python.exe scripts\repair_event_chain.py

需要 DG_ADMIN_DSN（管理员连接串）；应用控制账号不允许 UPDATE 事件表。
"""

from __future__ import annotations

import sys

from core.config import get_settings
from core.tasks.repository import _from_db, _json_load, event_hash
from database.mysql.connection import MysqlClient


def main() -> int:
    settings = get_settings()
    if settings.admin_dsn is None:
        print("缺少 DG_ADMIN_DSN，无法修复事件链")
        return 1
    admin = MysqlClient(
        settings.admin_dsn,
        database_id="dg_admin",
        read_only=False,
        connect_timeout_s=settings.connect_timeout_s,
    )
    rows = admin.fetch_all(
        "SELECT * FROM dg_system.task_events ORDER BY task_id, seq ASC"
    )
    tasks: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        tasks.setdefault(str(row["task_id"]), []).append(row)

    for task_id, task_rows in tasks.items():
        previous: str | None = None
        for row in task_rows:
            created = _from_db(row["created_at"])
            payload = _json_load(row.get("payload_json"), {})
            seq = int(row["seq"])
            new_hash = event_hash(
                task_id=task_id,
                run_id=str(row["run_id"]),
                tool_call_id=row.get("tool_call_id"),
                event_type=str(row["event_type"]),
                payload=payload,
                created_at=created,
                seq=seq,
                previous_event_hash=previous,
            )
            admin.execute(
                "UPDATE dg_system.task_events SET previous_event_hash=%s, event_hash=%s "
                "WHERE event_id=%s AND task_id=%s",
                (previous, new_hash, str(row["event_id"]), task_id),
            )
            previous = new_hash

    print(f"事件链修复完成：tasks={len(tasks)} events={len(rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
