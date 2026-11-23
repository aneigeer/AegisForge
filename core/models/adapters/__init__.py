"""模型适配器：离线桩与 OpenAI 兼容 HTTP 实现。"""

from core.models.adapters.base import ModelAdapter
from core.models.adapters.fake import FakeModelAdapter, HeuristicDiagnosticAdapter
from core.models.adapters.openai_compat import OpenAICompatibleAdapter

__all__ = [
    "FakeModelAdapter",
    "HeuristicDiagnosticAdapter",
    "ModelAdapter",
    "OpenAICompatibleAdapter",
]
