"""用户级记忆服务：短期会话上下文、长期偏好、个人问答对与时间衰减。

设计约束：
- 所有长期记忆按 user_id 隔离；当前版本被取代时软失效，不覆盖审计历史。
- 偏好是否生效由确定性规则决定，不让 LLM 改写记忆。
- 图表切换只复用 answer 的结果快照，不重新执行 SQL。
"""

import json
import logging
import math
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from chatbi.config import (
    MEMORY_EXPLICIT_HALF_LIFE_DAYS,
    MEMORY_INFERRED_HALF_LIFE_DAYS,
    MEMORY_MIN_CONFIDENCE,
    MEMORY_SESSION_TTL_HOURS,
    MEMORY_TERM_PROMOTE_SESSIONS,
)
from chatbi.db import get_meta_conn

logger = logging.getLogger(__name__)

CURRENT_STATUSES = ("active", "candidate")
CHART_TYPES = {
    "line", "multi_line", "bar", "bar_horizontal", "top10_bar_horizontal",
    "pie", "treemap", "metric_card", "table", "pivot_table", "waterfall",
}
CHART_NAMES = {
    "line": "折线图", "multi_line": "多系列折线图", "bar": "柱状图",
    "bar_horizontal": "条形图", "top10_bar_horizontal": "Top10条形图",
    "pie": "饼图", "treemap": "矩形树图", "metric_card": "指标卡",
    "table": "表格", "pivot_table": "透视表", "waterfall": "瀑布图",
}
CHART_ALIASES = {
    "多系列折线图": "multi_line", "折线图": "line", "柱状图": "bar",
    "条形图": "bar_horizontal", "饼图": "pie", "矩形树图": "treemap",
    "指标卡": "metric_card", "透视表": "pivot_table", "表格": "table",
}
FAMILY_NAMES = {
    "trend": "趋势分析", "category": "分类对比", "proportion": "占比分析",
    "single_metric": "单指标", "detail": "明细", "other": "其他数据",
}


def _id(prefix: str = "mem") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except (TypeError, ValueError):
        return None


def _json(value, default=None):
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (json.JSONDecodeError, TypeError):
        return default


def _close(conn: sqlite3.Connection, own: bool) -> None:
    if own:
        conn.close()


def _event(conn: sqlite3.Connection, memory_id: str, user_id: str, event_type: str,
           session_id: str | None = None, detail: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO user_memory_event (id, memory_id, user_id, event_type, session_id, "
        "detail_json) VALUES (?, ?, ?, ?, ?, ?)",
        (_id("mev"), memory_id, user_id, event_type, session_id,
         json.dumps(detail or {}, ensure_ascii=False, default=str)),
    )


def effective_confidence(row: sqlite3.Row | dict, now: datetime | None = None) -> float:
    """按最后确认时间指数衰减；普通读取/应用不会给记忆续期。"""
    row = dict(row)
    now = now or _now()
    confirmed = _parse_time(row.get("last_confirmed_at")) \
        or _parse_time(row.get("created_at")) or now
    days = max(0.0, (now - confirmed).total_seconds() / 86400)
    half_life = MEMORY_EXPLICIT_HALF_LIFE_DAYS if row.get("source") == "explicit" \
        else MEMORY_INFERRED_HALF_LIFE_DAYS
    if half_life <= 0:  # 配置为 0 时视为关闭衰减，避免除零
        return float(row.get("confidence", 1.0))
    base = max(0.0, min(1.0, float(row.get("confidence", 1.0))))
    return base * math.pow(0.5, days / half_life)


def _public_memory(row: sqlite3.Row | dict, now: datetime | None = None) -> dict:
    item = dict(row)
    item["value"] = _json(item.get("value_json"), {})
    item["effective_confidence"] = round(effective_confidence(item, now), 4)
    item["decayed"] = item["status"] == "active" \
        and item["effective_confidence"] < MEMORY_MIN_CONFIDENCE
    item["chart_name"] = CHART_NAMES.get(item["value"].get("chart_type")) \
        if isinstance(item["value"], dict) else None
    item["family_name"] = FAMILY_NAMES.get(item["memory_key"]) \
        if item["category"] == "chart_preference" else None
    item.pop("value_json", None)
    return item


def _current(conn: sqlite3.Connection, user_id: str, category: str,
             memory_key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM user_memory WHERE user_id = ? AND category = ? AND memory_key = ? "
        "AND status IN ('active', 'candidate')",
        (user_id, category, memory_key),
    ).fetchone()


def _validate_memory(category: str, memory_key: str, value: dict) -> None:
    if category not in ("chart_preference", "term_mapping", "query_exemplar"):
        raise ValueError("不支持的记忆类别")
    if not isinstance(memory_key, str) or not memory_key.strip() or len(memory_key) > 256:
        raise ValueError("记忆键不能为空且不能超过 256 字")
    if not isinstance(value, dict) or not value:
        raise ValueError("记忆内容必须是非空对象")
    if len(json.dumps(value, ensure_ascii=False, default=str)) > 100_000:
        raise ValueError("单条记忆内容过大")
    if category == "chart_preference":
        if memory_key not in FAMILY_NAMES:
            raise ValueError("未知的图表数据形态")
        if value.get("chart_type") not in CHART_TYPES:
            raise ValueError("未知的图表类型")
    elif category == "term_mapping":
        if not isinstance(value.get("term_id"), str) or not value["term_id"]:
            raise ValueError("术语映射缺少 term_id")
    elif category == "query_exemplar":
        if not isinstance(value.get("semql"), dict) or not value.get("nl_question"):
            raise ValueError("问答对记忆缺少问题或 SemQL")


def _save_memory(conn: sqlite3.Connection, user_id: str, memory_type: str, category: str,
                 memory_key: str, value: dict, source: str, status: str, confidence: float,
                 session_id: str | None = None) -> sqlite3.Row:
    """保存当前记忆；同值确认、异值软取代。调用方负责最后 commit。"""
    _validate_memory(category, memory_key, value)
    memory_key = memory_key.strip()
    if status not in CURRENT_STATUSES:
        raise ValueError("记忆初始状态只能是 active/candidate")
    confidence = max(0.0, min(1.0, float(confidence)))
    now = _iso(_now())
    # 新事务先取写锁，让“查旧→取代→插入”在并发请求下保持原子
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    old = _current(conn, user_id, category, memory_key)
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)

    if old is not None:
        old_value = json.dumps(_json(old["value_json"], {}), ensure_ascii=False,
                               sort_keys=True, default=str)
        if old_value == canonical:
            # 显式确认可提升候选；推断行为不会把显式来源降级
            new_source = old["source"] if old["source"] == "explicit" and source != "explicit" \
                else source
            new_status = "active" if status == "active" else old["status"]
            new_confidence = max(float(old["confidence"]), confidence)
            conn.execute(
                "UPDATE user_memory SET status = ?, source = ?, confidence = ?, "
                "last_confirmed_at = ?, updated_at = ? WHERE id = ?",
                (new_status, new_source, new_confidence, now, now, old["id"]),
            )
            _event(conn, old["id"], user_id, "confirmed", session_id,
                   {"category": category, "key": memory_key})
            return conn.execute("SELECT * FROM user_memory WHERE id = ?", (old["id"],)).fetchone()

        conn.execute(
            "UPDATE user_memory SET status = 'superseded', valid_to = ?, updated_at = ? "
            "WHERE id = ?", (now, now, old["id"]),
        )
        _event(conn, old["id"], user_id, "superseded", session_id,
               {"category": category, "key": memory_key, "new_value": value})

    memory_id = _id()
    conn.execute(
        """
        INSERT INTO user_memory (
            id, user_id, memory_type, category, memory_key, value_json, confidence,
            source, status, valid_from, last_confirmed_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (memory_id, user_id, memory_type, category, memory_key, canonical, confidence,
         source, status, now, now, now, now),
    )
    _event(conn, memory_id, user_id, "created", session_id,
           {"category": category, "key": memory_key, "value": value})
    return conn.execute("SELECT * FROM user_memory WHERE id = ?", (memory_id,)).fetchone()


def remember_preference(user_id: str, category: str, memory_key: str, value: dict,
                        source: str = "explicit", session_id: str | None = None,
                        conn: sqlite3.Connection | None = None) -> dict:
    """显式保存图表/术语偏好，立即生效。"""
    if category not in ("chart_preference", "term_mapping"):
        raise ValueError("该类别不能通过偏好接口写入")
    own = conn is None
    conn = conn or get_meta_conn()
    try:
        row = _save_memory(conn, user_id, "semantic", category, memory_key, value,
                           source, "active", 1.0 if source == "explicit" else 0.8,
                           session_id)
        conn.commit()
        return _public_memory(row)
    finally:
        _close(conn, own)


def observe_term_choice(user_id: str, mention: str, term_id: str, session_id: str,
                        conn: sqlite3.Connection | None = None) -> dict:
    """记录澄清选择；同一选择在指定数量的不同会话出现后晋升为长期偏好。"""
    if not session_id:
        raise ValueError("澄清观察必须携带 session_id")
    own = conn is None
    conn = conn or get_meta_conn()
    try:
        row = _save_memory(conn, user_id, "semantic", "term_mapping", mention,
                           {"term_id": term_id}, "clarify", "candidate", 0.6, session_id)
        # _save_memory 的同值确认已留事件；另记 observed 用于统计不同会话
        duplicate = conn.execute(
            "SELECT 1 FROM user_memory_event WHERE memory_id = ? AND event_type = 'observed' "
            "AND session_id = ? LIMIT 1", (row["id"], session_id),
        ).fetchone()
        if not duplicate:
            _event(conn, row["id"], user_id, "observed", session_id,
                   {"mention": mention, "term_id": term_id})
        count = conn.execute(
            "SELECT COUNT(DISTINCT session_id) AS n FROM user_memory_event "
            "WHERE memory_id = ? AND event_type = 'observed' AND session_id IS NOT NULL",
            (row["id"],),
        ).fetchone()["n"]
        promoted = False
        if row["status"] == "candidate" and count >= MEMORY_TERM_PROMOTE_SESSIONS:
            now = _iso(_now())
            conn.execute(
                "UPDATE user_memory SET status = 'active', confidence = 0.85, "
                "last_confirmed_at = ?, updated_at = ? WHERE id = ?", (now, now, row["id"]),
            )
            _event(conn, row["id"], user_id, "promoted", session_id,
                   {"sessions": count})
            promoted = True
        conn.commit()
        current = conn.execute("SELECT * FROM user_memory WHERE id = ?", (row["id"],)).fetchone()
        return {"memory": _public_memory(current), "promoted": promoted, "sessions": count}
    finally:
        _close(conn, own)


def get_term_preferences(user_id: str, snapshot,
                         conn: sqlite3.Connection | None = None) -> dict[str, dict]:
    """返回仍可用的长期术语映射；尚未晋升和已衰减记忆不参与消歧。"""
    own = conn is None
    conn = conn or get_meta_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM user_memory WHERE user_id = ? AND category = 'term_mapping' "
            "AND status = 'active'", (user_id,),
        ).fetchall()
        result: dict[str, dict] = {}
        for row in rows:
            value = _json(row["value_json"], {})
            term_id = value.get("term_id")
            term = snapshot.terms.get(term_id)
            if effective_confidence(row) < MEMORY_MIN_CONFIDENCE or not term:
                continue
            if term.get("term_type") != "metric_term" or not snapshot.metric_of_term(term_id):
                continue
            result[f"term:{row['memory_key']}"] = {
                "selected_key": term_id, "label": term.get("name", term_id),
                "memory_id": row["id"], "confidence": effective_confidence(row),
            }
        return result
    finally:
        _close(conn, own)


def mark_accessed(memory_id: str, user_id: str, session_id: str | None = None,
                  conn: sqlite3.Connection | None = None) -> None:
    """只更新访问统计，不刷新 last_confirmed_at，防止旧记忆自我续期。"""
    own = conn is None
    conn = conn or get_meta_conn()
    try:
        now = _iso(_now())
        conn.execute(
            "UPDATE user_memory SET last_accessed_at = ?, access_count = access_count + 1 "
            "WHERE id = ? AND user_id = ?", (now, memory_id, user_id),
        )
        _event(conn, memory_id, user_id, "applied", session_id)
        conn.commit()
    finally:
        _close(conn, own)


def list_memories(user_id: str, include_history: bool = False,
                  conn: sqlite3.Connection | None = None) -> list[dict]:
    own = conn is None
    conn = conn or get_meta_conn()
    try:
        where = "" if include_history else "AND status IN ('active', 'candidate')"
        rows = conn.execute(
            f"SELECT * FROM user_memory WHERE user_id = ? {where} "
            "ORDER BY updated_at DESC, created_at DESC", (user_id,),
        ).fetchall()
        return [_public_memory(row) for row in rows]
    finally:
        _close(conn, own)


def delete_memory(user_id: str, memory_id: str,
                  conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    conn = conn or get_meta_conn()
    try:
        row = conn.execute("SELECT * FROM user_memory WHERE id = ?", (memory_id,)).fetchone()
        if row is None:
            raise KeyError("记忆不存在")
        if row["user_id"] != user_id:
            raise PermissionError("只能管理自己的记忆")
        if row["status"] not in CURRENT_STATUSES:
            raise ValueError("该记忆已经失效，无需重复删除")
        now = _iso(_now())
        conn.execute(
            "UPDATE user_memory SET status = 'deleted', valid_to = ?, updated_at = ? WHERE id = ?",
            (now, now, memory_id),
        )
        _event(conn, memory_id, user_id, "deleted")
        conn.commit()
    finally:
        _close(conn, own)


def clear_memories(user_id: str, category: str | None = None,
                   conn: sqlite3.Connection | None = None) -> int:
    own = conn is None
    conn = conn or get_meta_conn()
    try:
        params: list = [user_id]
        extra = ""
        if category:
            extra = " AND category = ?"
            params.append(category)
        rows = conn.execute(
            f"SELECT * FROM user_memory WHERE user_id = ? {extra} "
            "AND status IN ('active', 'candidate')", params,
        ).fetchall()
        now = _iso(_now())
        for row in rows:
            conn.execute(
                "UPDATE user_memory SET status = 'deleted', valid_to = ?, updated_at = ? "
                "WHERE id = ?", (now, now, row["id"]),
            )
            _event(conn, row["id"], user_id, "deleted", detail={"category": row["category"]})
        conn.commit()
        return len(rows)
    finally:
        _close(conn, own)


# ---------- 短期会话上下文 ----------

def _clean_context(state: dict) -> dict:
    """只接受白名单字段，损坏/超大内容按字段丢弃而不是拖垮对话。"""
    if not isinstance(state, dict):
        return {"turns": [], "last_semql": None, "clarify_choices": {}, "last_answer": None}
    turns = []
    for turn in (state.get("turns") or [])[-5:]:
        if isinstance(turn, dict):
            turns.append({
                "question": str(turn.get("question", ""))[:2000],
                "intent": str(turn.get("intent", ""))[:64],
                "semql_brief": str(turn.get("semql_brief", ""))[:2000],
            })
    choices = {}
    raw_choices = state.get("clarify_choices")
    if isinstance(raw_choices, dict):
        for key, value in list(raw_choices.items())[:50]:
            if isinstance(key, str) and isinstance(value, str) and len(key) <= 300 \
                    and len(value) <= 300:
                choices[key] = value
    last_semql = state.get("last_semql") if isinstance(state.get("last_semql"), dict) else None
    if last_semql and len(json.dumps(last_semql, ensure_ascii=False, default=str)) > 50_000:
        last_semql = None
    last_answer = state.get("last_answer")
    if not isinstance(last_answer, dict) or not isinstance(last_answer.get("answer_id"), str):
        last_answer = None
    else:
        last_answer = {"answer_id": last_answer["answer_id"][:64],
                       "family": str(last_answer.get("family", "other"))[:32]}
    return {"turns": turns, "last_semql": last_semql,
            "clarify_choices": choices, "last_answer": last_answer}


def load_session_context(session_id: str, user_id: str,
                         conn: sqlite3.Connection | None = None) -> dict | None:
    own = conn is None
    conn = conn or get_meta_conn()
    try:
        row = conn.execute(
            """
            SELECT c.* FROM chat_context c
            JOIN chat_session s ON s.id = c.session_id
            WHERE c.session_id = ? AND c.user_id = ? AND s.user_id = ?
            """, (session_id, user_id, user_id),
        ).fetchone()
        if row is None:
            return None
        expires = _parse_time(row["expires_at"])
        if expires is None or expires <= _now():
            conn.execute("DELETE FROM chat_context WHERE session_id = ?", (session_id,))
            conn.commit()
            return None
        state = _json(row["state_json"], None)
        if not isinstance(state, dict):
            logger.warning("会话上下文 JSON 损坏，按空上下文恢复: %s", session_id)
            conn.execute("DELETE FROM chat_context WHERE session_id = ?", (session_id,))
            conn.commit()
            return None
        return _clean_context(state)
    finally:
        _close(conn, own)


def save_session_context(session_id: str, user_id: str, state: dict,
                         conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    conn = conn or get_meta_conn()
    try:
        owner = conn.execute("SELECT user_id FROM chat_session WHERE id = ?",
                             (session_id,)).fetchone()
        if owner is None:
            raise KeyError("会话不存在")
        if owner["user_id"] != user_id:
            raise PermissionError("只能保存自己的会话上下文")
        cleaned = _clean_context(state)
        now = _now()
        expires = _iso(now + timedelta(hours=max(1, MEMORY_SESSION_TTL_HOURS)))
        conn.execute(
            """
            INSERT INTO chat_context (session_id, user_id, state_json, expires_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                user_id = excluded.user_id, state_json = excluded.state_json,
                expires_at = excluded.expires_at,
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
            """,
            (session_id, user_id, json.dumps(cleaned, ensure_ascii=False, default=str), expires),
        )
        conn.commit()
    finally:
        _close(conn, own)


def clear_session_context(session_id: str, user_id: str,
                          conn: sqlite3.Connection | None = None) -> bool:
    own = conn is None
    conn = conn or get_meta_conn()
    try:
        cursor = conn.execute(
            "DELETE FROM chat_context WHERE session_id = ? AND user_id = ?",
            (session_id, user_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        _close(conn, own)


# ---------- 图表偏好 ----------

def chart_family(columns_meta: list[dict], semql: dict | None = None) -> str:
    semql = semql or {}
    if semql.get("query_mode") == "detail":
        return "detail"
    times = [c for c in columns_meta if c.get("role") == "time"]
    dims = [c for c in columns_meta if c.get("role") == "dimension"]
    metrics = [c for c in columns_meta if c.get("role") == "metric"]
    if times:  # 与图表规则优先级一致：时间序列优先于占比语义
        return "trend"
    if any(any(word in c.get("name", "") for word in ("占比", "份额", "率", "%"))
           for c in metrics):
        return "proportion"
    if dims:
        return "category"
    if len(metrics) == 1:
        return "single_metric"
    return "other"


def requested_chart_type(question: str | None) -> str | None:
    """普通问题中的图型词只影响当次结果；否定表达不视为指定。"""
    if not question:
        return None
    for alias, chart_type in sorted(CHART_ALIASES.items(), key=lambda x: len(x[0]), reverse=True):
        for match in re.finditer(re.escape(alias), question):
            prefix = question[max(0, match.start() - 12):match.start()]
            if re.search(r"(?:不要|不用|别(?:再)?用|不希望|不喜欢)[^，。！？]{0,8}$", prefix):
                continue
            if alias in ("表格", "透视表", "指标卡") and not re.search(
                    r"(?:用|换成|改成|展示为|显示为|生成|画成|输出为|切换为)[^，。！？]{0,5}$",
                    prefix):
                continue  # “表格”等普通名词必须有明确展示动词，避免劫持数据问题
            return chart_type
    return None


def personalize_chart(user_id: str, chart: dict, columns_meta: list[dict], semql: dict,
                      question: str | None = None, session_id: str | None = None,
                      conn: sqlite3.Connection | None = None) -> tuple[dict, list[str]]:
    """当次指定优先，其次长期偏好；目标类型必须仍在规则引擎的可选集合内。"""
    own = conn is None
    conn = conn or get_meta_conn()
    try:
        result = dict(chart or {})
        result["available_types"] = list(result.get("available_types") or [result.get("type")])
        family = chart_family(columns_meta, semql)
        result["family"] = family
        traces: list[str] = []
        requested = requested_chart_type(question)
        requested_name = CHART_NAMES.get(requested, requested)
        if requested:
            if requested in result["available_types"]:
                result["type"] = requested
                result["reason"] = f"{result.get('reason', '')}；用户当次指定{requested_name}"
                traces.append(f"按您当次要求使用{requested_name}（未写入长期偏好）")
            else:
                traces.append(f"当前数据不支持{requested_name}，已保留规则推荐图表")
            return result, traces

        row = _current(conn, user_id, "chart_preference", family)
        if row is not None and row["status"] == "active" \
                and effective_confidence(row) >= MEMORY_MIN_CONFIDENCE:
            value = _json(row["value_json"], {})
            preferred = value.get("chart_type")
            if preferred in result["available_types"]:
                result["type"] = preferred
                result["reason"] = f"{result.get('reason', '')}；用户长期偏好"
                name = CHART_NAMES.get(preferred, preferred)
                traces.append(
                    f"已沿用您的{FAMILY_NAMES[family]}图表偏好：{name}（可在记忆管理中修改）")
                now = _iso(_now())
                conn.execute(
                    "UPDATE user_memory SET last_accessed_at = ?, "
                    "access_count = access_count + 1 WHERE id = ?", (now, row["id"]),
                )
                _event(conn, row["id"], user_id, "applied", session_id,
                       {"family": family, "chart_type": preferred})
                conn.commit()
        return result, traces
    finally:
        _close(conn, own)


def switch_answer_chart(user_id: str, answer_id: str, chart_type: str,
                        session_id: str | None = None, source: str = "chart_switch",
                        conn: sqlite3.Connection | None = None) -> dict:
    """切换本人历史回答的图表并学习偏好；不重新生成或执行 SQL。"""
    if chart_type not in CHART_TYPES:
        raise ValueError("未知的图表类型")
    own = conn is None
    conn = conn or get_meta_conn()
    try:
        row = conn.execute(
            """
            SELECT a.*, q.user_id, q.session_id, q.question, q.semql_json,
                   q.final_sql, q.applied_rules, q.semantic_version
            FROM answer a JOIN query_log q ON q.id = a.query_log_id
            WHERE a.id = ?
            """, (answer_id,),
        ).fetchone()
        if row is None:
            raise KeyError("图表回答不存在")
        if row["user_id"] != user_id:
            raise PermissionError("只能切换自己的图表")
        old_chart = _json(row["chart_meta"], {}) or {}
        available = list(old_chart.get("available_types") or [row["chart_type"], "table"])
        if chart_type not in available:
            names = "、".join(CHART_NAMES.get(t, t) for t in available if t)
            raise ValueError(f"当前数据不支持{CHART_NAMES.get(chart_type, chart_type)}，可选：{names}")
        snapshot = _json(row["result_snapshot"], None)
        columns_meta = _json(row["columns_meta"], [])
        semql = _json(row["semql_json"], {})
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("columns"), list) \
                or not isinstance(snapshot.get("rows"), list) \
                or not isinstance(columns_meta, list) or not isinstance(semql, dict):
            raise ValueError("历史结果快照不完整，请重新查询后再切换")

        family = chart_family(columns_meta, semql)
        new_chart = dict(old_chart)
        new_chart.update({
            "type": chart_type,
            "reason": f"{old_chart.get('reason', '')}；用户切换并记忆",
            "available_types": available,
            "family": family,
        })
        conn.execute(
            "UPDATE answer SET chart_type = ?, chart_meta = ? WHERE id = ?",
            (chart_type, json.dumps(new_chart, ensure_ascii=False), answer_id),
        )
        memory = _save_memory(
            conn, user_id, "semantic", "chart_preference", family,
            {"chart_type": chart_type}, source, "active",
            1.0 if source == "explicit" else 0.8, session_id or row["session_id"],
        )
        conn.commit()
        preference_note = (
            f"已切换为{CHART_NAMES[chart_type]}，并记住您的{FAMILY_NAMES[family]}图表偏好")
        result = {
            "answer_type": "data", "answer_id": answer_id, "question": row["question"],
            "semql": semql, "sql": row["final_sql"],
            "applied_rules": _json(row["applied_rules"], []) or [],
            "columns": snapshot["columns"], "columns_meta": columns_meta,
            "rows": snapshot["rows"], "row_count": row["row_count"],
            "truncated": bool(row["row_count"] and row["row_count"] > len(snapshot["rows"])),
            "chart": new_chart, "summary": row["summary_text"],
            "assumptions": [preference_note], "memory_trace": [preference_note],
            "semantic_version": row["semantic_version"], "memory_switch": True,
        }
        return {"chart": new_chart, "memory": _public_memory(memory),
                "memory_trace": [preference_note], "result": result}
    finally:
        _close(conn, own)


# ---------- 个人问答对（情景记忆） ----------

def _question_key(question: str) -> str:
    key = " ".join((question or "").split()).lower()
    if not key or len(key) > 2000:
        raise ValueError("问答对问题为空或过长")
    return key


def remember_exemplar(user_id: str, answer_id: str,
                      conn: sqlite3.Connection | None = None) -> dict | None:
    own = conn is None
    conn = conn or get_meta_conn()
    try:
        row = conn.execute(
            """
            SELECT q.user_id, q.session_id, q.question, q.semql_json FROM answer a
            JOIN query_log q ON q.id = a.query_log_id
            WHERE a.id = ? AND q.status = 'success'
            """, (answer_id,),
        ).fetchone()
        if row is None:
            return None
        if row["user_id"] != user_id:
            raise PermissionError("只能记忆自己的问答对")
        semql = _json(row["semql_json"], None)
        if not isinstance(semql, dict):
            return None
        question = row["question"]
        saved = _save_memory(
            conn, user_id, "episodic", "query_exemplar", _question_key(question),
            {"dataset_id": semql.get("dataset", "ds_sales"), "nl_question": question,
             "semql": semql}, "feedback", "active", 1.0,
            row["session_id"] if "session_id" in row.keys() else None,
        )
        conn.commit()
        return _public_memory(saved)
    finally:
        _close(conn, own)


def forget_exemplar(user_id: str, answer_id: str,
                    conn: sqlite3.Connection | None = None) -> bool:
    own = conn is None
    conn = conn or get_meta_conn()
    try:
        row = conn.execute(
            """
            SELECT q.user_id, q.question FROM answer a
            JOIN query_log q ON q.id = a.query_log_id WHERE a.id = ?
            """, (answer_id,),
        ).fetchone()
        if row is None:
            return False
        if row["user_id"] != user_id:
            raise PermissionError("只能管理自己的问答对")
        current = _current(conn, user_id, "query_exemplar", _question_key(row["question"]))
        if current is None:
            return False
        now = _iso(_now())
        conn.execute(
            "UPDATE user_memory SET status = 'deleted', valid_to = ?, updated_at = ? WHERE id = ?",
            (now, now, current["id"]),
        )
        _event(conn, current["id"], user_id, "deleted", detail={"reason": "feedback_down"})
        conn.commit()
        return True
    finally:
        _close(conn, own)


def pick_personal_exemplars(user_id: str, question: str, dataset_id: str, top: int = 3,
                            conn: sqlite3.Connection | None = None) -> list[dict]:
    """个人问答对召回：沿用项目现有字面重合度，叠加有效置信度排序。"""
    own = conn is None
    conn = conn or get_meta_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM user_memory WHERE user_id = ? AND category = 'query_exemplar' "
            "AND status = 'active'", (user_id,),
        ).fetchall()
        ranked = []
        for row in rows:
            value = _json(row["value_json"], {})
            if value.get("dataset_id") != dataset_id:
                continue
            confidence = effective_confidence(row)
            if confidence < MEMORY_MIN_CONFIDENCE:
                continue
            nl_question = value.get("nl_question", "")
            relevance = sum(1 for ch in set(question) if ch in nl_question)
            if relevance <= 0 or not isinstance(value.get("semql"), dict):
                continue
            score = relevance * 0.7 + confidence * 0.3
            ranked.append((score, row, value))
        ranked.sort(key=lambda item: item[0], reverse=True)
        picked = []
        now = _iso(_now())
        for score, row, value in ranked[:max(0, top)]:
            picked.append({
                "id": row["id"], "memory_id": row["id"], "dataset_id": dataset_id,
                "nl_question": value["nl_question"], "semql_json": value["semql"],
                "source": "user_memory", "memory_score": round(score, 4),
            })
            conn.execute(
                "UPDATE user_memory SET last_accessed_at = ?, "
                "access_count = access_count + 1 WHERE id = ?", (now, row["id"]),
            )
            _event(conn, row["id"], user_id, "applied", detail={"question": question})
        if picked:
            conn.commit()
        return picked
    finally:
        _close(conn, own)
