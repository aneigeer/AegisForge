"""V3.0 业务域插件包（Domain Plugin SDK）。

``contracts`` 定义四大强制接口与跨域数据模型；各业务域子包
（``database`` / ``k8s`` / ``host`` / ``appsec``）提供符合契约的实现。
"""

from __future__ import annotations

__all__ = ["contracts"]
