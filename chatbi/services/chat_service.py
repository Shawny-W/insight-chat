"""对话服务编排（契约见 spec 第 6 节：ask(user, session_id, question) -> 事件生成器）。

事件序列（PRD 6.1.4）：intent → clarify（挂起）/ progress → result / error。
会话上下文：SQLite chat_session/chat_message + 进程内 dict 缓存 last_semql。
"""

import json
import logging
import re
import uuid
from typing import Iterator

from chatbi.chart.rules import choose
from chatbi.db import get_meta_conn
from chatbi.insight.attribution import AttributionError, attribute
from chatbi.insight.fluctuation import detect
from chatbi.insight.narrative import narrate
from chatbi.llm import chat_text
from chatbi.nlp.clarify import MAX_CLARIFY_ROUNDS, build_clarify, detect_ambiguities
from chatbi.nlp.intent import Intent, IntentResult, classify
from chatbi.nlp.linking import link
from chatbi.nlp.rewrite import rewrite
from chatbi.query.compiler import CompileError
from chatbi.query.gateway import SqlForbiddenError
from chatbi.query.generator import LLMGenerateFailedError
from chatbi.query.semql import resolve_time_range
from chatbi.nlp.intent import today
from chatbi.semantic.store import load_snapshot
from chatbi.services import history
from chatbi.services.auth import get_user_policies
from chatbi.services.query_service import run_query

logger = logging.getLogger(__name__)

# 进程内会话上下文缓存（session_id → {turns, last_semql, clarify_pending}）
_CTX: dict[str, dict] = {}


def _ev(event: str, data: dict) -> dict:
    return {"event": event, "data": data}


def _get_ctx(session_id: str) -> dict:
    return _CTX.setdefault(session_id,
                           {"turns": [], "last_semql": None, "clarify_pending": None})


# ---------- 归因问题的期间解析 ----------

_MONTH_RE = re.compile(r"(?:(\d{4})\s*年)?(\d{1,2})\s*月")


def _parse_why_periods(question: str, intent: IntentResult) -> tuple[str, str, list[str]]:
    """从 why 问题解析 (period_a, period_b)；缺失按默认（最近完整月 vs 上一完整月）+假设。

    年份推断：无年份的月份继承问题中显式出现的年份，否则用当前年。
    """
    assumptions: list[str] = []
    year_in_q = None
    m_y = re.search(r"(\d{4})\s*年", question)
    if m_y:
        year_in_q = int(m_y.group(1))
    default_year = year_in_q or today().year

    hits: list[str] = []
    for m in _MONTH_RE.finditer(question):
        y = int(m.group(1)) if m.group(1) else default_year
        hits.append(f"{y}-{int(m.group(2)):02d}")
    if len(hits) >= 2:
        return hits[0], hits[1], assumptions
    if len(hits) == 1 and intent.time_range:
        return intent.time_range["start"][:7], hits[0], assumptions
    # 默认：最近完整月 vs 上一完整月（以当前日期）
    t = today()
    first_of_month = t.replace(day=1)
    from datetime import timedelta
    last_month_end = first_of_month - timedelta(days=1)
    prev_month_end = last_month_end.replace(day=1) - timedelta(days=1)
    assumptions.append(
        f"未指定比较期，按默认「最近完整月 vs 上一完整月」："
        f"{prev_month_end.strftime('%Y-%m')} vs {last_month_end.strftime('%Y-%m')}")
    return prev_month_end.strftime("%Y-%m"), last_month_end.strftime("%Y-%m"), assumptions


def _pick_metric(snapshot, link_result, default: str = "metric_qty") -> str:
    for m in link_result.matched_elements:
        if m["element_type"] == "term":
            t = snapshot.terms.get(m["element_id"])
            if t and t.get("term_type") == "metric_term":
                metric = snapshot.metric_of_term(m["element_id"])
                if metric:
                    return metric["id"]
    return default


# ---------- 结果摘要（light 模型，失败跳过） ----------

def _summarize(question: str, result: dict) -> str | None:
    try:
        preview = json.dumps({"columns": result["columns"],
                              "rows": result["rows"][:5]}, ensure_ascii=False, default=str)
        return chat_text(
            "result_summary",
            "你是BI助手，用一句中文大白话总结查询结果（不超过60字，含关键数字）。只输出这句话。",
            f"问题：{question}\n结果：{preview}",
            heavy=False, temperature=0.3)
    except Exception as exc:  # noqa: BLE001
        logger.warning("结果摘要生成失败（跳过）: %s", exc)
        return None


def _columns_meta(result: dict) -> list[dict]:
    """从 compile trace 组装列角色（time/dimension/metric/column）。"""
    trace = result.get("trace", {})
    meta: list[dict] = []
    dim_aliases = {v["alias"] for v in trace.get("dimensions", {}).values()}
    metric_aliases = {v["alias"] for v in trace.get("metrics", {}).values()}
    grain_alias = {"month": "月份", "week": "周", "day": "日期"}.get(
        (trace.get("time") or {}).get("grain"))
    for name in result.get("columns", []):
        if grain_alias and name == grain_alias:
            meta.append({"name": name, "role": "time"})
        elif name in dim_aliases:
            meta.append({"name": name, "role": "dimension"})
        elif name in metric_aliases or name.endswith(("_基期", "_差值")) or "%" in name:
            meta.append({"name": name, "role": "metric"})
        else:
            meta.append({"name": name, "role": "column"})
    return meta


# ---------- 澄清挂起的续走 ----------

def clarify(user: dict, clarify_id: str, selected_key: str | None = None,
            free_text: str | None = None) -> Iterator[dict]:
    """澄清响应后续走查询链路（契约见 spec）。"""
    pending = None
    for sid, ctx in _CTX.items():
        p = ctx.get("clarify_pending")
        if p and p["clarify_id"] == clarify_id:
            pending = (sid, ctx, p)
            break
    if pending is None:
        yield _ev("error", {"code": "CLARIFY_EXPIRED",
                            "message": "澄清已过期，请重新提问", "retryable": False})
        return
    session_id, ctx, p = pending
    question = p["question"]

    if free_text:
        ctx["clarify_pending"] = None
        yield from ask(user, session_id, f"{question}（补充：{free_text}）")
        return

    # 合并选择
    intent: IntentResult = p["intent"]
    link_result = p["link_result"]
    if p["type"] == "TERM_CONFLICT" and selected_key:
        link_result.matched_elements = [
            m for m in link_result.matched_elements
            if not (m["element_type"] == "term"
                    and m["mention"] == p["mention"]
                    and m["element_id"] != selected_key)
        ]
        # 兜底：用户所选 term 若不在命中列表里（如仅来自 LLM 候选），补一条命中，
        # 确保下游 prompt 注入该术语口径、LLM 按所选指标出 SemQL
        if not any(m["element_type"] == "term" and m["element_id"] == selected_key
                   for m in link_result.matched_elements):
            link_result.matched_elements.append(
                {"element_type": "term", "element_id": selected_key,
                 "mention": p["mention"] or ""})
    elif p["type"] == "VALUE_CONFLICT" and selected_key:
        dim_id, _, tech = selected_key.partition("|")
        link_result.matched_elements = [
            m for m in link_result.matched_elements
            if not (m["element_type"] == "dim_value"
                    and m["mention"] == p["mention"]
                    and not (m["element_id"] == dim_id
                             and m.get("tech_value") == tech))
        ]
    elif p["type"] == "TIME_MISSING" and selected_key:
        try:
            s, e = resolve_time_range(selected_key, today())
            intent.time_range = {"start": s, "end": e}
        except ValueError:
            pass
    elif p["type"] == "GRAIN_MISSING" and selected_key:
        intent.grain = selected_key
    ctx["clarify_pending"] = None
    yield from _run_data_query(user, session_id, question, intent, link_result,
                               ctx, clarified=True)


# ---------- 主入口 ----------

def ask(user: dict, session_id: str | None, question: str) -> Iterator[dict]:
    """ask(user, session_id, question) -> 事件生成器（契约签名固定）。"""
    conn = get_meta_conn()
    try:
        if session_id is None:
            session_id = history.create_session(user["id"], question[:20], conn)
        else:
            s = conn.execute("SELECT * FROM chat_session WHERE id = ?",
                             (session_id,)).fetchone()
            if s is None:
                session_id = history.create_session(user["id"], question[:20], conn)
            elif s["user_id"] != user["id"]:
                yield _ev("error", {"code": "FORBIDDEN",
                                    "message": "无权访问该会话", "retryable": False})
                return
        history.save_message(session_id, "user", question, conn=conn)
    finally:
        conn.close()

    ctx = _get_ctx(session_id)
    snapshot = load_snapshot()

    # 指代改写（有上下文时）
    rewritten, inherits, topic_changed = rewrite(question, ctx.get("turns") or [])
    if topic_changed:
        ctx["last_semql"] = None

    # 意图识别
    try:
        intent = classify(rewritten, ctx.get("turns") or [], snapshot)
    except Exception as exc:  # noqa: BLE001
        yield _ev("error", {"code": "INTENT_FAILED",
                            "message": f"意图识别失败: {exc}", "retryable": True})
        return
    yield _ev("intent", {"intent": intent.intent.value, "mentions": intent.mentions,
                         "time_range": intent.time_range, "grain": intent.grain,
                         "degraded": intent.degraded, "assumptions": [],
                         "session_id": session_id, "question": rewritten,
                         "rewritten": rewritten != question, "inherits": inherits})

    # 闲聊/不可答/写操作
    if intent.intent == Intent.CHITCHAT:
        yield _result_text(user, session_id, ctx, intent,
                           "您好，我是 InsightChat 数据助手。您可以问我销售数据相关的问题，"
                           "例如「2024年4月各大区的净销售额」。")
        return
    if intent.intent == Intent.UNSUPPORTED:
        yield _result_text(user, session_id, ctx, intent,
                           "我只支持数据查询，不能执行修改或删除操作。"
                           "您可以这样问我：「2024年4月各大区的净销售额」。")
        return
    if intent.intent == Intent.FORBIDDEN_DOMAIN:
        yield _result_text(user, session_id, ctx, intent,
                           "您没有查询该数据域的权限。如需开通，请联系数据管理员 admin。")
        return

    # schema linking
    yield _ev("progress", {"stage": "schema_linking"})
    link_result = link(rewritten, intent.mentions, snapshot, intent.dataset_id)

    # 歧义复核 → 澄清
    ambiguities = detect_ambiguities(rewritten, intent, link_result, snapshot)
    clarify_round = (ctx.get("clarify_pending", {}) or {}).get("round", 0) + 1
    if ambiguities and clarify_round <= MAX_CLARIFY_ROUNDS:
        card = build_clarify(ambiguities, snapshot, round=clarify_round)
        if card is not None:
            # 卡片取自「按优先级排序后的首个」歧义，mention 必须取同一个，
            # 否则 clarify 合并时按错误的 mention 过滤会导致用户选择失效
            chosen_amb = next((a for a in ambiguities if a["type"] == card.type), {})
            ctx["clarify_pending"] = {
                "clarify_id": card.clarify_id, "question": rewritten, "intent": intent,
                "link_result": link_result, "round": clarify_round,
                "type": card.type,
                "mention": chosen_amb.get("mention"),
            }
            conn = get_meta_conn()
            try:
                history.save_message(session_id, "assistant", card.prompt,
                                     intent=intent.intent.value,
                                     clarify_info=card.to_dict(), conn=conn)
            finally:
                conn.close()
            yield _ev("clarify", card.to_dict())
            return

    # 分派
    if intent.intent in (Intent.INSIGHT_WHY, Intent.INSIGHT_ANOMALY_CHECK):
        yield from _run_insight(user, session_id, rewritten, intent, link_result, ctx)
    else:
        yield from _run_data_query(user, session_id, rewritten, intent, link_result, ctx)


def _run_data_query(user: dict, session_id: str, question: str, intent: IntentResult,
                    link_result, ctx: dict, clarified: bool = False) -> Iterator[dict]:
    """DATA_QUERY 链路：generate→compile→gateway→execute→chart→summary。"""
    snapshot = load_snapshot()
    assumptions: list[str] = []
    time_range = intent.time_range
    if time_range is None:
        ds = snapshot.datasets.get(intent.dataset_id or "") or next(
            iter(snapshot.datasets.values()))
        default_range = (ds or {}).get("default_time_range")
        if default_range:
            s, e = resolve_time_range(default_range, today())
            time_range = {"start": s, "end": e}
            assumptions.append(f"已按默认时间范围：{default_range}（{s} ~ {e}）查询")

    try:
        yield _ev("progress", {"stage": "generating"})
        result = run_query(question, user_id=user["id"], mentions=intent.mentions,
                           time_range=time_range, grain=intent.grain,
                           ctx_turns=ctx.get("turns"), session_id=session_id,
                           snapshot=snapshot,
                           # 澄清续走：复用已按用户选择过滤的 link_result；
                           # 否则传 None 由 run_query 内部重新 link（保持原有行为）
                           link_result=link_result if clarified else None)
    except LLMGenerateFailedError as exc:
        yield _ev("error", {"code": "LLM_GENERATE_FAILED",
                            "message": "这个问题有点复杂，我暂时无法准确理解。"
                                       "您可以换个说法，或试试：「2024年4月各大区的净销售额」",
                            "retryable": False})
        return
    except SqlForbiddenError as exc:
        yield _ev("error", {"code": exc.code,
                            "message": "该查询包含不允许的操作，已被拦截"
                            if exc.code == "SQL_FORBIDDEN" else exc.message,
                            "retryable": False})
        return
    except CompileError as exc:
        yield _ev("error", {"code": exc.code, "message": exc.message, "retryable": False})
        return
    except Exception as exc:  # noqa: BLE001
        yield _ev("error", {"code": "INTERNAL", "message": f"系统开小差了: {exc}",
                            "retryable": True})
        return

    yield _ev("progress", {"stage": "executing"})
    cols_meta = _columns_meta(result)
    chart = choose(cols_meta, result["rows"], result["semql"])
    summary = _summarize(question, result)

    policies = get_user_policies(user["id"])
    explain = _build_match_explain(result["matched_elements"], snapshot, policies)

    conn = get_meta_conn()
    try:
        answer_id = history.save_answer(
            result["query_log_id"], chart["type"], chart, cols_meta,
            {"columns": result["columns"], "rows": result["rows"][:200]},
            result["row_count"], summary, conn=conn)
        history.save_message(session_id, "assistant",
                             summary or f"查询完成，共 {result['row_count']} 行",
                             intent=intent.intent.value, conn=conn)
    finally:
        conn.close()

    ctx["last_semql"] = result["semql"]
    ctx["turns"] = (ctx.get("turns") or [])[-4:] + [
        {"question": question, "intent": intent.intent.value,
         "semql_brief": _semql_brief(result["semql"])}]

    yield _ev("result", {
        "answer_type": "data",
        "answer_id": answer_id,
        "question": question,
        "semql": result["semql"], "sql": result["sql"],
        "applied_rules": result["applied_rules"],
        "columns": result["columns"], "columns_meta": cols_meta,
        "rows": result["rows"], "row_count": result["row_count"],
        "truncated": result["truncated"],
        "chart": chart, "summary": summary,
        "match_explain": explain,
        "assumptions": assumptions + result.get("assumptions", []),
        "semantic_version": result["semantic_version"],
        "clarified": clarified,
    })


def _run_insight(user: dict, session_id: str, question: str, intent: IntentResult,
                 link_result, ctx: dict) -> Iterator[dict]:
    """INSIGHT_WHY / INSIGHT_ANOMALY_CHECK 链路：fluctuation → attribution → narrative。"""
    snapshot = load_snapshot()
    policies = get_user_policies(user["id"])
    flat_policies = policies["row_policies"] + policies["column_policies"]
    metric_id = _pick_metric(snapshot, link_result)
    period_a, period_b, assumptions = _parse_why_periods(question, intent)
    base_filters = [{"dimension": "dim_channel", "op": "not_in", "values": ["TEST"]}]

    yield _ev("progress", {"stage": "executing", "note": "正在确认对比口径…"})
    try:
        fluc = detect(metric_id, period_a, period_b, snapshot, flat_policies, base_filters)
    except Exception as exc:  # noqa: BLE001
        yield _ev("error", {"code": "ATTRIB_FAILED", "message": f"波动识别失败: {exc}",
                            "retryable": True})
        return

    term = snapshot.term_of_metric(metric_id) or {}
    metric_name = term.get("name", metric_id)

    # 对比期数据为空（PRD 6.6.7#2）：链路终止
    if fluc["V_A"] == 0 or fluc["V_B"] == 0:
        empty_period = period_a if fluc["V_A"] == 0 else period_b
        yield _ev("result", {
            "answer_type": "insight", "question": question,
            "is_significant": False, "fluctuation": fluc,
            "summary": f"{empty_period} 没有{metric_name}数据，无法进行归因。请确认数据是否已入库。",
            "assumptions": assumptions, "semantic_version": snapshot.version,
        })
        return

    if intent.intent == Intent.INSIGHT_ANOMALY_CHECK:
        if fluc["is_significant"]:
            text = (f"{metric_name} {fluc['period_a'].split('/')[0][:7]} 波动超出正常区间"
                    f"（环比 {fluc['pct']}%，历史均值 {fluc['mu']}%，σ={fluc['sigma']}%），"
                    f"判定为异常。")
        else:
            text = (f"{metric_name} 处于正常波动区间（环比 {fluc['pct']}%，"
                    f"历史均值 {fluc['mu']}%±2σ）。")
        yield _ev("result", {
            "answer_type": "anomaly", "question": question,
            "is_significant": fluc["is_significant"], "fluctuation": fluc,
            "summary": text, "assumptions": assumptions,
            "metric_id": metric_id, "semantic_version": snapshot.version,
        })
        return

    if not fluc["is_significant"]:
        yield _ev("result", {
            "answer_type": "insight", "question": question,
            "is_significant": False, "fluctuation": fluc,
            "summary": f"{metric_name} {fluc['period_a'].split('/')[0][:7]} "
                       f"较基期变化 {fluc['pct']}%，处于正常波动区间（±2σ内），无需归因。",
            "assumptions": assumptions, "semantic_version": snapshot.version,
        })
        return

    yield _ev("progress", {"stage": "executing", "note": "正在计算各维度贡献…"})
    try:
        result = attribute(metric_id, period_a, period_b, base_filters,
                           snapshot, flat_policies)
    except AttributionError as exc:
        yield _ev("error", {"code": exc.code, "message": exc.message, "retryable": False})
        return
    except Exception as exc:  # noqa: BLE001
        yield _ev("error", {"code": "ATTRIB_FAILED", "message": f"归因计算失败: {exc}",
                            "retryable": True})
        return

    yield _ev("progress", {"stage": "executing", "note": "正在关联业务事件…"})
    report = result.to_dict()

    yield _ev("progress", {"stage": "rendering", "note": "正在生成结论…"})
    narrative, degraded = narrate(report)

    conn = get_meta_conn()
    try:
        rid = f"attr_{uuid.uuid4().hex[:12]}"
        conn.execute(
            "INSERT INTO insight_report (id, user_id, session_id, metric_id, period_a, "
            "period_b, status, report_json, narrative, semantic_version) "
            "VALUES (?, ?, ?, ?, ?, ?, 'completed', ?, ?, ?)",
            (rid, user["id"], session_id, metric_id,
             report["period"]["A"], report["period"]["B"],
             json.dumps(report, ensure_ascii=False, default=str), narrative,
             snapshot.version))
        history.save_message(session_id, "assistant", narrative,
                             intent=intent.intent.value, conn=conn)
        conn.commit()
    finally:
        conn.close()

    ctx["turns"] = (ctx.get("turns") or [])[-4:] + [
        {"question": question, "intent": intent.intent.value,
         "semql_brief": f"归因:{metric_name} {period_a} vs {period_b}"}]

    yield _ev("result", {
        "answer_type": "insight", "question": question,
        "report_id": rid, "metric_id": metric_id,
        "report": report, "narrative": narrative,
        "narrative_degraded": degraded,
        "chart": {"type": "waterfall", "reason": "rule_11: 归因结果",
                  "available_types": ["waterfall", "table"]},
        "assumptions": assumptions,
        "semantic_version": snapshot.version,
    })


# ---------- 辅助 ----------

def _result_text(user, session_id, ctx, intent, text) -> dict:
    conn = get_meta_conn()
    try:
        history.save_message(session_id, "assistant", text,
                             intent=intent.intent.value, conn=conn)
    finally:
        conn.close()
    ctx["turns"] = (ctx.get("turns") or [])[-4:] + [
        {"question": "", "intent": intent.intent.value}]
    return _ev("result", {"answer_type": "text", "summary": text, "assumptions": []})


def _semql_brief(semql: dict) -> str:
    metrics = ",".join(semql.get("metrics", []))
    dims = ",".join(semql.get("dimensions", []))
    t = (semql.get("time") or {}).get("range", "")
    return f"metrics[{metrics}]×dims[{dims}]×time[{t}]"


def _build_match_explain(matched: list[dict], snapshot, policies: dict) -> list[str]:
    """匹配解释区：已识别元素 + 数据范围说明。"""
    parts = []
    type_name = {"term": "指标/术语", "dimension": "维度", "dim_value": "维度值",
                 "dataset": "数据集"}
    for m in matched:
        label = type_name.get(m["element_type"], m["element_type"])
        name = m["element_id"]
        if m["element_type"] == "term" and m["element_id"] in snapshot.terms:
            name = snapshot.terms[m["element_id"]]["name"]
        elif m["element_type"] == "dimension" and m["element_id"] in snapshot.dimensions:
            name = snapshot.dimensions[m["element_id"]]["display_name"]
        elif m["element_type"] == "dim_value":
            name = m["mention"]
        parts.append(f"{name}（{label}，匹配自“{m['mention']}”）")
    for p in policies.get("row_policies", []):
        dim = snapshot.dimensions.get(p.get("dimension_id"))
        if dim:
            labels = [vm["display_value"] for vm in dim.get("value_maps", [])
                      if vm["tech_value"] in (p.get("allowed_values") or [])]
            parts.append(f"数据范围：{'、'.join(labels) or p.get('allowed_values')}（{dim['display_name']}）")
    return parts
