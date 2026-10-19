"""意图识别（契约见 IMPLEMENTATION_SPEC.md 第 6 节）。

LLM（light 模型）为主，失败重试 1 次（temperature 0.2），仍失败降级为关键词规则意图。
输出时间一律规范化为 {start, end} 字面量（当前日期按 TZ_NAME 注入 prompt）。
"""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from zoneinfo import ZoneInfo

from chatbi.config import TZ_NAME
from chatbi.llm import LLMUnavailableError, chat_json
from chatbi.nlp import prompts
from chatbi.semantic.dictionary import segment
from chatbi.semantic.store import SemanticSnapshot

logger = logging.getLogger(__name__)


class Intent(str, Enum):
    DATA_QUERY = "DATA_QUERY"
    INSIGHT_WHY = "INSIGHT_WHY"
    INSIGHT_ANOMALY_CHECK = "INSIGHT_ANOMALY_CHECK"
    CHITCHAT = "CHITCHAT"
    UNSUPPORTED = "UNSUPPORTED"
    FORBIDDEN_DOMAIN = "FORBIDDEN_DOMAIN"


@dataclass
class IntentResult:
    intent: Intent
    confidence: float = 0.0
    mentions: list[str] = field(default_factory=list)
    time_range: dict | None = None      # {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}
    grain: str | None = None            # day/week/month/None
    ambiguities: list[dict] = field(default_factory=list)
    dataset_id: str | None = None
    reasoning_brief: str = ""
    degraded: bool = False              # True=规则兜底（LLM 失败）


def today() -> date:
    return datetime.now(ZoneInfo(TZ_NAME)).date()


# ---------- 关键词规则兜底（PRD 6.1.5#1 降级路径） ----------

_WHY_WORDS = ("为什么", "原因", "怎么降", "哪里出了问题", "怎么涨", "为何")
_ANOMALY_WORDS = ("正常吗", "有没有异常", "波动", "异常吗")
_WRITE_WORDS = ("删了", "删除", "删掉", "清空", "删表", "drop", "truncate", "insert into",
                "update ", "改掉", "修改数据")


def rule_classify(question: str, snapshot: SemanticSnapshot) -> IntentResult:
    """规则兜底意图：仅做意图分类与 mentions 词典抽取，不做时间猜测。"""
    q = question.lower()
    if any(w in q for w in _WRITE_WORDS):
        intent = Intent.UNSUPPORTED
    elif any(w in q for w in _WHY_WORDS):
        intent = Intent.INSIGHT_WHY
    elif any(w in q for w in _ANOMALY_WORDS):
        intent = Intent.INSIGHT_ANOMALY_CHECK
    else:
        intent = Intent.DATA_QUERY
    mentions = [w for w, nature in segment(question) if nature.startswith("_")]
    dataset_id = next(iter(snapshot.datasets), None)
    return IntentResult(intent=intent, confidence=0.5, mentions=mentions,
                        dataset_id=dataset_id, degraded=True,
                        reasoning_brief="LLM 不可用，规则兜底识别")


# ---------- 时间表达式规范化（Python 兜底解析） ----------

def _month_range(y: int, m: int) -> tuple[str, str]:
    if m == 12:
        nxt = date(y + 1, 1, 1)
    else:
        nxt = date(y, m + 1, 1)
    from datetime import timedelta
    return f"{y}-{m:02d}-01", (nxt - timedelta(days=1)).isoformat()


def parse_time_expr(expr: str | None, ref: date | None = None) -> tuple[dict, str | None] | None:
    """把常见中文时间表达解析为 ({"start","end"}, grain)；无法解析返回 None。"""
    if not expr:
        return None
    ref = ref or today()
    s = str(expr).strip()

    m = re.fullmatch(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        ds = f"{y}-{mo:02d}-{d:02d}"
        return {"start": ds, "end": ds}, "day"
    m = re.fullmatch(r"(\d{4})[-/年](\d{1,2})月?(?:\s*到\s*(\d{1,2})月?)?", s)
    if m:
        y, m1 = int(m.group(1)), int(m.group(2))
        if m.group(3):
            m2 = int(m.group(3))
            start = f"{y}-{m1:02d}-01"
            _, end = _month_range(y, m2)
            return {"start": start, "end": end}, "month"
        start, end = _month_range(y, m1)
        return {"start": start, "end": end}, "month"
    m = re.fullmatch(r"(\d{1,2})\s*月", s)
    if m:
        y = ref.year
        start, end = _month_range(y, int(m.group(1)))
        return {"start": start, "end": end}, "month"
    m = re.fullmatch(r"(\d{4})\s*年", s)
    if m:
        y = int(m.group(1))
        return {"start": f"{y}-01-01", "end": f"{y}-12-31"}, None
    if s in ("上个月", "上月"):
        first = date(ref.year, ref.month, 1)
        from datetime import timedelta
        last_of_prev = first - timedelta(days=1)
        start, end = _month_range(last_of_prev.year, last_of_prev.month)
        return {"start": start, "end": end}, "month"
    if s in ("这个月", "本月", "当月"):
        start, end = _month_range(ref.year, ref.month)
        return {"start": start, "end": end}, "month"
    if s == "今年":
        return {"start": f"{ref.year}-01-01", "end": f"{ref.year}-12-31"}, None
    if s == "去年":
        return {"start": f"{ref.year - 1}-01-01", "end": f"{ref.year - 1}-12-31"}, None
    return None


# ---------- LLM 意图识别 ----------

def _term_lines(snap: SemanticSnapshot) -> list[str]:
    lines = []
    for t in snap.terms.values():
        alias = f"（别名:{'/'.join(t.get('aliases', []))}）" if t.get("aliases") else ""
        lines.append(f"- {t['id']} {t['name']}{alias}")
    return lines


def _validate_time_range(obj: dict | None, ref: date) -> tuple[dict | None, str | None]:
    """校验/规范化 LLM 输出的 time_range。"""
    if not obj or not isinstance(obj, dict):
        return None, None
    start, end = obj.get("start"), obj.get("end")
    if not (isinstance(start, str) and isinstance(end, str)):
        return None, None
    if not (re.fullmatch(r"\d{4}-\d{2}-\d{2}", start) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", end)):
        return None, None
    if start > end:
        start, end = end, start
    return {"start": start, "end": end}, None


def classify(question: str, ctx: list[dict], snapshot: SemanticSnapshot) -> IntentResult:
    """意图识别主入口（契约签名固定）。"""
    ref = today()
    ctx_lines = [f"前一轮: 问题={t.get('question', '')}" for t in (ctx or [])[-5:]]
    dataset_lines = [f"- {d['id']} {d['name']}：{d.get('description', '')}" for d in snapshot.datasets.values()]
    rule_keywords = sorted({kw for r in snapshot.rules.values() for kw in r.get("trigger_keywords", [])})
    user = prompts.intent_user(question, ctx_lines, ref.isoformat(),
                               _term_lines(snapshot), rule_keywords, dataset_lines)

    raw: dict | None = None
    for attempt, temp in enumerate((0.0, 0.2)):  # 失败重试 1 次
        try:
            raw = chat_json("intent", prompts.INTENT_SYSTEM, user, heavy=False, temperature=temp)
            break
        except (LLMUnavailableError, Exception) as exc:  # LLMFormatError 也在此重试
            logger.warning("意图识别第 %d 次调用失败: %s", attempt + 1, exc)
    if raw is None or "intent" not in raw:
        return rule_classify(question, snapshot)

    try:
        intent = Intent(str(raw.get("intent", "DATA_QUERY")))
    except ValueError:
        intent = Intent.DATA_QUERY

    time_range, _ = _validate_time_range(raw.get("time_range"), ref)
    grain = raw.get("grain") if raw.get("grain") in ("day", "week", "month") else None
    if time_range is None:
        parsed = parse_time_expr(raw.get("time_expr"), ref)
        if parsed:
            time_range, pgrain = parsed
            grain = grain or pgrain

    # mentions：LLM 输出 ∪ 词典切出的命中词
    mentions = [m for m in raw.get("mentions", []) if isinstance(m, str) and m.strip()]
    for w, nature in segment(question):
        if nature.startswith("_") and w not in mentions:
            mentions.append(w)

    dataset_id = raw.get("dataset_id")
    if dataset_id not in snapshot.datasets:
        dataset_id = next(iter(snapshot.datasets), None)

    ambiguities = [a for a in raw.get("ambiguities", []) if isinstance(a, dict)]
    return IntentResult(
        intent=intent,
        confidence=float(raw.get("confidence") or 0.8),
        mentions=mentions,
        time_range=time_range,
        grain=grain,
        ambiguities=ambiguities,
        dataset_id=dataset_id,
        reasoning_brief=str(raw.get("reasoning_brief", "")),
    )
