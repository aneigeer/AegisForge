"""W11 安全回归与长稳（``core/assurance``）。

本包是 W11 六张卡（T01…T06）的唯一落点，与 W10 的 ``core/benchmark`` 完全不相交。

纪律（``docs/w11/03-task-cards.md`` §全局纪律）：
- **零生产枚举改动**：不改 ``core/errors.py`` / ``core/audit/events.py``；
- **只读边界**：负面集只做"诱导被拒"，不新增可写工具、不新增 API 端点、
  不发起真实网络连接；
- **不重写上游**：``percentile`` / ``canonical_digest`` / 策略引擎 / 审批九项校验
  只复用，不在本包另造一套。

``ASSURANCE_PACKAGE_VERSION`` 由 T01 冻结，**后续卡（T02…T06）不得修改本文件**。
"""

from __future__ import annotations

ASSURANCE_PACKAGE_VERSION = "w11-v1"

__all__ = ["ASSURANCE_PACKAGE_VERSION"]
