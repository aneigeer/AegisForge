"""W09 T03 安全渲染（AC-12）：全部纯文本化，不做标签白名单正则清洗。

设计 §4.4：报告渲染允许的标签集合 = 空集；``render_markdown`` 只生成
``#`` / ``-`` / 文本 / 表格，``to_html`` 只做 ``<pre>`` 包裹的纯文本转义。

``sanitize_text`` = ``html.escape(quote=True)`` + 去控制字符 + 中和内联可执行
字面量（``javascript:`` / ``onerror=``）。后者是**纯文本化**的一部分，不是
标签白名单正则清洗：本模块不使用 ``re`` 做标签放行，也没有允许标签集合。
"""

from __future__ import annotations

import html
import unicodedata

from core.report.models import StructuredReport

# 需要中和的内联可执行字面量（大小写不敏感）；替换成实体形式后仍是纯文本。
_NEUTRALIZE: tuple[tuple[str, str], ...] = (
    ("javascript:", "javascript&#58;"),
    ("onerror=", "onerror&#61;"),
)


def _replace_case_insensitive(text: str, needle: str, replacement: str) -> str:
    """大小写不敏感地逐段替换；纯字符串扫描，不使用正则做标签清洗。"""
    lowered = text.casefold()
    target = needle.casefold()
    parts: list[str] = []
    index = 0
    while True:
        hit = lowered.find(target, index)
        if hit < 0:
            parts.append(text[index:])
            return "".join(parts)
        parts.append(text[index:hit])
        parts.append(replacement)
        index = hit + len(target)


def sanitize_text(value: str) -> str:
    """把任意模型/证据字符串转成可安全展示的纯文本。

    1. 去掉控制字符（保留换行与制表符，便于 Markdown 排版）；
    2. ``html.escape(quote=True)`` 转义 ``< > & " '``，``<script`` / ``< script``
       变体一律变 ``&lt;...``；
    3. 中和 ``javascript:`` / ``onerror=``，使渲染结果不含这两类可执行字面量。
    """
    cleaned = "".join(
        char
        for char in value
        if char in "\n\t" or unicodedata.category(char) not in ("Cc", "Cf")
    )
    escaped = html.escape(cleaned, quote=True)
    for needle, replacement in _NEUTRALIZE:
        escaped = _replace_case_insensitive(escaped, needle, replacement)
    return escaped


def _report_lines(report: StructuredReport) -> list[str]:
    """未转义的原始行；由两个渲染入口各自调用 ``sanitize_text``。"""
    evidence_ids = ", ".join(report.supporting_evidence_ids) or "无"
    contradicting = ", ".join(report.contradicting_evidence_ids) or "无"
    missing = ", ".join(report.missing_evidence) or "无"
    recommendations = " / ".join(
        f"{item.text}（{item.state.value}）" for item in report.recommendations
    ) or "无"
    return [
        f"# 结构化报告 {report.report_id}",
        "",
        f"- 报告版本: {report.report_version}",
        f"- 租户 / 任务: {report.tenant_id} / {report.task_id}",
        f"- 数据库 / 引擎 / 版本: {report.database_id} / {report.db_engine} / {report.db_version}",
        f"- Adapter 版本 / 环境: {report.adapter_version} / {report.environment_id}",
        f"- 策略版本 / 适用范围: {report.policy_version} / {report.scope}",
        "",
        "## 结论",
        report.conclusion,
        "",
        "## 状态",
        "| 项 | 值 |",
        "| --- | --- |",
        f"| 事实等级 | {report.fact_status.value} |",
        f"| 动作状态 | {report.action_state.value} |",
        f"| 验证状态 | {report.verification_state.value} |",
        f"| 置信带 | {report.confidence_band.value} |",
        f"| 降级 | {report.degraded} |",
        f"| 已发布 | {report.published} |",
        "",
        "## 证据与建议",
        f"- 支持证据: {evidence_ids}",
        f"- 反证: {contradicting}",
        f"- 缺失项: {missing}",
        f"- 建议: {recommendations}",
    ]


def render_markdown(report: StructuredReport) -> str:
    """只生成 ``#`` / ``-`` / 文本 / 表格；所有字符串先 ``sanitize_text``。"""
    return "\n".join(sanitize_text(line) for line in _report_lines(report))


def to_html(report: StructuredReport) -> str:
    """只包 ``<pre>`` 的纯文本转义；不生成任何其他 HTML 标签。"""
    plain = "\n".join(_report_lines(report))
    return "<pre>" + sanitize_text(plain) + "</pre>"


__all__ = ["render_markdown", "sanitize_text", "to_html"]
