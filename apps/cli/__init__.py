"""AegisForge CLI 包（V4.1）：面向本地开发流与 CI 的门禁入口。

刻意不在包级 import 子模块——``python -m apps.cli.secops_gate`` 若在 ``__init__``
里提前导入会触发 "found in sys.modules after import of package" 的重复导入告警。
使用方请直接 ``from apps.cli.secops_gate import main``。
"""

__all__: list[str] = []
