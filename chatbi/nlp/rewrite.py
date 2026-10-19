"""多轮指代改写（契约见 spec 第 6 节：返回 (rewritten_question, inherits, topic_changed)）。

无上下文直接原样返回；LLM 失败按单轮处理（原样返回）。
"""

import logging

from chatbi.llm import chat_json
from chatbi.nlp import prompts

logger = logging.getLogger(__name__)


def _ctx_lines(ctx_turns: list[dict]) -> list[str]:
    lines = []
    for t in ctx_turns[-5:]:
        q = t.get("question", "")
        brief = t.get("semql_brief") or ""
        lines.append(f"前一轮: 问题={q}" + (f"；查询摘要={brief}" if brief else ""))
    return lines


def rewrite(question: str, ctx_turns: list[dict] | None) -> tuple[str, bool, bool]:
    """返回 (改写后问题, 是否继承上文, 是否换了话题)。"""
    if not ctx_turns:
        return question, False, False
    try:
        raw = chat_json("rewrite", prompts.REWRITE_SYSTEM,
                        prompts.rewrite_user(question, _ctx_lines(ctx_turns)),
                        heavy=False, temperature=0.0)
        rewritten = str(raw.get("rewritten") or question).strip()
        inherits = bool(raw.get("inherits", False))
        topic_changed = bool(raw.get("topic_changed", False))
        if topic_changed:
            inherits = False
        if not rewritten:
            rewritten = question
        return rewritten, inherits, topic_changed
    except Exception as exc:  # noqa: BLE001 - 改写失败降级为单轮处理
        logger.warning("指代改写失败，按单轮处理: %s", exc)
        return question, False, False
