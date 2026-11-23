"""W12 发布包公共类型（T01 创建；后续卡**不得修改本文件**）。

本模块只承载两件事：

1. ``RELEASE_PACKAGE_VERSION``：发布包版本权威常量（后续卡引用，不重复定义）；
2. :class:`AcceptanceStatus`：**四值**验收状态闭集，是 T01–T06 全部验收结果件的
   公共枚举。

四值语义（`docs/w12/02-design.md` §4.1、`docs/w10-w12-lead-rulings.md` D-09）：

- ``PASSED``：本轮实测通过；
- ``FAILED``：本轮实测不通过；
- ``NOT_EXECUTED``：环境阻塞（如 ``ENV-PG5432`` / ``ENV-NV1``）导致**未执行**，
  **不判 FAIL**，也**不得**冒充通过；
- ``NOT_AVAILABLE``：上游结果件未落地/不可读（D-09），**不判 FAIL**，如实登记。

> 本模块**不含**任何演示字段（``demo_passed`` / ``demo_status`` / ``demo_ok``），
> 因为演示结果不属于验收状态的任何一档（AC-20）。
"""

from __future__ import annotations

from enum import Enum

#: 发布包版本权威常量；后续卡只读引用，不得修改本文件（任务卡 T01 关键签名）。
RELEASE_PACKAGE_VERSION: str = "w12-v1"


class AcceptanceStatus(str, Enum):
    """验收状态闭集（四值）；新增取值须走版本化评审。"""

    PASSED = "PASSED"
    FAILED = "FAILED"
    #: 环境阻塞导致未执行：不判 FAIL、不冒充通过。
    NOT_EXECUTED = "NOT_EXECUTED"
    #: 上游结果件未落地/不可读：不判 FAIL，登记为遗留（D-09）。
    NOT_AVAILABLE = "NOT_AVAILABLE"


__all__ = ["RELEASE_PACKAGE_VERSION", "AcceptanceStatus"]
