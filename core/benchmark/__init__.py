"""W10 评测基座（**离线评测工具；不进生产镜像、不新增 API 端点**）。

本包只承载离线评测与"数据不泄漏"相关工具，按任务卡硬顺序逐卡追加模块：

- ``oracle.py``（T01）：Oracle 隔离底座 —— 静态导入白名单、终态闸门、
  单次读取、审计留痕、待外发文本泄漏扫描；
- ``manifest.py`` / ``runner.py`` / ``__main__.py``（T02）、``metrics.py`` /
  ``stats.py``（T03）、``dataset.py`` / ``tokens.py``（T04）、``telemetry.py``
  （T05）、``report.py``（T06）由后续卡交付。

边界（任务卡「全局纪律」2/10/11、主理人裁定 Q-W10-B）：

- 代码落 ``core/benchmark/``，随 ``core*`` 打包，``pyproject.toml`` 零改动；
- 只依赖 ``core*`` / ``database*`` / ``adapters*`` / ``benchmarks*``；
  **禁止** import ``apps/**``、``workers/**``、``core/gateway/**``、
  ``core/approval/**``、``core/agent/**``；
- 不新增第三方依赖、不新增前端、不新增 API 端点、不新增 exporter 服务；
- 评测进程只读，不扩大写能力；Oracle 真值不得进入日志、异常 details、
  审计 payload 或任何运行产物。

口径（W10-Q1）：本包属「离线可判工作」，**不代表 W10 开工**，也不代表
上游 W09 门禁已评或已通过。
"""

BENCHMARK_PACKAGE_VERSION = "w10-v1"

__all__ = ["BENCHMARK_PACKAGE_VERSION"]
