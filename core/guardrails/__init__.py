"""V4.1 Track B：框架无关的通用 Agent 安全护栏 SDK。

设计目标：把 AegisForge 已有的三块能力**原样复用**给任意外部 AI Agent 项目——

- 双向可逆脱敏：:class:`core.privacy_gateway.multimodal.MultimodalPrivacyGateway`
  （Fernet 信封加密持久化映射，PII 令牌永不还原）;
- 高危动作策略门控：本包的 :mod:`core.guardrails.policies`；
- 防篡改审计链：本包 :class:`AgentGuardrailClient.export_audit_chain`。

刻意**不**依赖 LangChain / LlamaIndex 等重型框架：只用标准库与项目既有核心依赖，
这样外部项目 `pip install` 本项目后可以直接 ``from core.guardrails import ...``。

最小接入（5 行内）：

```python
from core.guardrails import AgentGuardrailClient, guard_tool_call

client = AgentGuardrailClient()
decision = client.guard_outbound("手机号 13800000000 的账单", workflow_run_id="wf-1")
masked = decision.masked_payload

@guard_tool_call(risk_tier="P0")
def drop_table(name, hitl_approved=False, workflow_run_id=""):
    return f"dropped {name}"
```
"""

from core.guardrails.gateway import (
    AgentGuardrailClient,
    GuardrailBlockedError,
    GuardrailDecision,
    GuardrailError,
    GuardrailRestoreError,
    configure_default_client,
    default_client,
    export_audit_chain,
    guard_tool_call,
    verify_audit_chain,
)
from core.guardrails.policies import (
    BLOCKING_CATEGORIES,
    DefaultAgentSafetyPolicy,
    PolicyFinding,
    PolicyRule,
)

__all__ = [
    "AgentGuardrailClient",
    "BLOCKING_CATEGORIES",
    "DefaultAgentSafetyPolicy",
    "GuardrailBlockedError",
    "GuardrailDecision",
    "GuardrailError",
    "GuardrailRestoreError",
    "PolicyFinding",
    "PolicyRule",
    "configure_default_client",
    "default_client",
    "export_audit_chain",
    "guard_tool_call",
    "verify_audit_chain",
]
