"""对话服务编排（契约见 spec 第 6 节：ask(user, session_id, question) -> 事件生成器）。

事件序列（PRD 6.1.4）：intent → clarify（挂起）/ progress → result / error。
会话上下文：SQLite chat_session/chat_message/chat_context + 进程内 OrderedDict LRU。
长期用户记忆契约见 docs/MEMORY_SYSTEM.md。
"""

from collections import OrderedDict
import json
import logging
import re
import time
import uuid
from typing import Iterator

from chatbi.chart.rules import choose
from chatbi.config import INSIGHT_SIGNIFICANT_PCT, INSIGHT_SIGMA_K, \
    MEMORY_CONTEXT_CACHE_SIZE, MEMORY_SESSION_TTL_HOURS
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
from chatbi.services import history, memory
from chatbi.services.auth import get_user_policies
from chatbi.services.query_service import run_query

logger = logging.getLogger(__name__)

# LRU 进程缓存只是加速层；可序列化状态同步落 chat_context，服务重启后可恢复
_CTX: OrderedDict[str, dict] = OrderedDict()


def _ev(event: str, data: dict) -> dict:
    return {"event": event, "data": data}


def _blank_ctx() -> dict:
    return {"turns": [], "last_semql": None, "clarify_pending": None,
            # 会话内已澄清选择记忆：{"term:<mention>": term_id,
            # "value:<mention>": "dim_id|tech"}，追问时沿用不再重复发问
            "clarify_choices": {}, "last_answer": None, "_last_used": time.time()}


def _get_ctx(session_id: str, user_id: str) -> dict:
    now = time.time()
    cached = _CTX.get(session_id)
    if cached is not None and now - cached.get("_last_used", now) \
            < MEMORY_SESSION_TTL_HOURS * 3600:
        cached["_last_used"] = now
        _CTX.move_to_end(session_id)
        return cached

    ctx = _blank_ctx()
    try:
        persisted = memory.load_session_context(session_id, user_id)
        if persisted:
            ctx.update(persisted)
    except Exception as exc:  # noqa: BLE001 - 持久化不可用时退回原有进程内上下文
        logger.warning("会话上下文恢复失败（使用空上下文）: %s", exc)
    ctx["_last_used"] = now
    _CTX[session_id] = ctx
    _CTX.move_to_end(session_id)

    # 优先淘汰无 pending 的最久未用会话，避免正在等待澄清的上下文被意外清掉
    while len(_CTX) > max(1, MEMORY_CONTEXT_CACHE_SIZE):
        victim = next((sid for sid, value in _CTX.items()
                       if sid != session_id and not value.get("clarify_pending")), None)
        if victim is None:
            break
        _CTX.pop(victim, None)
    return ctx


def _persist_ctx(user_id: str, session_id: str, ctx: dict) -> None:
    ctx["_last_used"] = time.time()
    state = {key: ctx.get(key) for key in
             ("turns", "last_semql", "clarify_choices", "last_answer")}
    try:
        memory.save_session_context(session_id, user_id, state)
    except Exception as exc:  # noqa: BLE001 - 持久化失败不阻断已完成的查询
        logger.warning("会话上下文保存失败（本次结果不受影响）: %s", exc)


def discard_session_memory(session_id: str, user_id: str) -> None:
    """删除会话时同步清理两级短期记忆。"""
    _CTX.pop(session_id, None)
    memory.clear_session_context(session_id, user_id)


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


# ---------- 用户记忆命令（严格匹配，不消耗 LLM，也不进入 NL2SQL） ----------

def _parse_memory_command(question: str, snapshot) -> dict | None:
    text = question.strip().rstrip("。！!？?")
    if re.fullmatch(r"(?:你)?(?:还记得|记得|记住了)(?:我)?(?:什么|哪些)(?:记忆|偏好|内容)?",
                    text) or re.fullmatch(r"(?:查看|看看)(?:我的)?(?:记忆|偏好)", text):
        return {"type": "list"}
    if re.fullmatch(r"(?:请)?(?:忘掉|忘记|删除)(?:我的)?(?:全部|所有)(?:记忆|偏好)(?:吧)?", text):
        return {"type": "forget_all"}
    if re.fullmatch(r"(?:请)?(?:忘掉|忘记|删除)(?:我的)?(?:全部|所有)?图表偏好(?:吧)?", text):
        return {"type": "forget_category", "category": "chart_preference"}
    if re.fullmatch(r"(?:请)?(?:忘掉|忘记|删除)(?:我的)?(?:全部|所有)?(?:问答|问答对)记忆(?:吧)?", text):
        return {"type": "forget_category", "category": "query_exemplar"}
    forget_term = re.fullmatch(
        r"(?:请)?(?:忘掉|忘记|删除)(?:我的)?[“\"]?([^”\"，,]+?)[”\"]?(?:的)?(?:口径|映射|术语偏好)",
        text)
    if forget_term:
        return {"type": "forget_term", "mention": forget_term.group(1).strip()}

    chart_pattern = "|".join(re.escape(name) for name in
                             sorted(memory.CHART_ALIASES, key=len, reverse=True))
    switch = re.fullmatch(
        rf"(?:请)?(?:(?:把|将)?(?:这张|这个|当前|上述|上面)?(?:图|图表)?)?"
        rf"(?:换成|改成|变为|切换为|生成|画成|用)({chart_pattern})", text)
    if switch:
        return {"type": "switch_chart",
                "chart_type": memory.CHART_ALIASES[switch.group(1)]}

    explicit_remember = re.match(r"^(?:请)?记住", text)
    future_preference = re.match(r"^(?:以后|今后)", text) and any(
        marker in text for marker in ("都用", "都按", "都指", "一直", "默认", "偏好"))
    # “以后/今后”也是正常业务问句的常见开头，只有明确偏好句式才接管
    if not explicit_remember and not future_preference:
        return None
    chart_type = next((kind for alias, kind in memory.CHART_ALIASES.items() if alias in text), None)
    if chart_type:
        family = None
        if any(word in text for word in ("趋势", "走势", "变化")):
            family = "trend"
        elif any(word in text for word in ("对比", "比较", "分类")):
            family = "category"
        elif any(word in text for word in ("占比", "构成", "比例")):
            family = "proportion"
        elif "单指标" in text:
            family = "single_metric"
        if family is None:
            return {"type": "memory_error",
                    "message": "请说明偏好的数据场景，例如「以后趋势都用折线图」或「以后分类对比都用柱状图」。"}
        return {"type": "remember_chart", "family": family, "chart_type": chart_type}

    term_match = re.fullmatch(
        r"(?:请)?(?:记住|以后|今后)(?:将|把)?\s*[“\"]?([^”\"，,]+?)[”\"]?\s*"
        r"(?:都)?(?:指|按|是|理解为)\s*[“\"]?([^”\"，,]+?)[”\"]?", text)
    if term_match:
        mention, target = (part.strip() for part in term_match.groups())
        if not mention or len(mention) > 64 or len(target) > 64:
            return {"type": "memory_error", "message": "术语名称不能为空且不能超过 64 字。"}
        exact, aliases = [], []
        for term_id, term in snapshot.terms.items():
            if term.get("term_type") != "metric_term":
                continue
            term_aliases = term.get("aliases") or []
            if isinstance(term_aliases, str):
                try:
                    term_aliases = json.loads(term_aliases)
                except json.JSONDecodeError:
                    term_aliases = []
            if term.get("name") == target:
                exact.append(term_id)
            elif target in term_aliases:
                aliases.append(term_id)
        candidates = exact or aliases
        if len(candidates) != 1:
            return {"type": "memory_error",
                    "message": f"找不到唯一指标「{target}」，请使用完整指标名，例如「净销售额」。"}
        return {"type": "remember_term", "mention": mention, "term_id": candidates[0]}
    if not explicit_remember:
        return None
    return {"type": "memory_error",
            "message": "我可以记住图表或指标口径偏好，例如「以后趋势都用折线图」"
                       "或「记住销售额指净销售额」。"}


def _memory_text(session_id: str, text: str, command: str,
                 trace: list[str] | None = None) -> dict:
    conn = get_meta_conn()
    try:
        history.save_message(session_id, "assistant", text, intent="MEMORY", conn=conn)
    finally:
        conn.close()
    return _ev("result", {"answer_type": "text", "summary": text,
                          "assumptions": [], "memory_trace": trace or [],
                          "memory_command": command})


def _handle_memory_command(user: dict, session_id: str, ctx: dict, command: dict,
                           snapshot) -> dict:
    """执行记忆命令并返回 result 事件；显式失败不会降级成数据查询。"""
    ctype = command["type"]
    if ctype == "memory_error":
        return _memory_text(session_id, command["message"], ctype)
    if ctype == "remember_chart":
        chart_name = memory.CHART_NAMES[command["chart_type"]]
        family_name = memory.FAMILY_NAMES[command["family"]]
        memory.remember_preference(
            user["id"], "chart_preference", command["family"],
            {"chart_type": command["chart_type"]}, source="explicit", session_id=session_id)
        text = f"已记住：您在{family_name}场景偏好{chart_name}，后续查询会自动沿用。"
        return _memory_text(session_id, text, ctype, [text])
    if ctype == "remember_term":
        term = snapshot.terms[command["term_id"]]
        memory.remember_preference(
            user["id"], "term_mapping", command["mention"],
            {"term_id": command["term_id"]}, source="explicit", session_id=session_id)
        ctx.setdefault("clarify_choices", {})[f"term:{command['mention']}"] = command["term_id"]
        _persist_ctx(user["id"], session_id, ctx)
        text = f"已记住：「{command['mention']}」按「{term['name']}」查询。"
        return _memory_text(session_id, text, ctype, [text])
    if ctype == "switch_chart":
        last_answer = ctx.get("last_answer") or {}
        if not last_answer.get("answer_id"):
            return _memory_text(session_id, "还没有可切换的图表，请先完成一次数据查询。", ctype)
        try:
            switched = memory.switch_answer_chart(
                user["id"], last_answer["answer_id"], command["chart_type"],
                session_id=session_id, source="explicit")
        except (KeyError, ValueError) as exc:
            return _memory_text(session_id, str(exc), ctype)
        result = switched["result"]
        ctx["last_answer"] = {"answer_id": last_answer["answer_id"],
                              "family": result["chart"].get("family", "other")}
        _persist_ctx(user["id"], session_id, ctx)
        conn = get_meta_conn()
        try:
            history.save_message(session_id, "assistant", switched["memory_trace"][0],
                                 intent="MEMORY", conn=conn)
        finally:
            conn.close()
        return _ev("result", result)
    if ctype == "list":
        items = memory.list_memories(user["id"])
        semantic = [item for item in items if item["memory_type"] == "semantic"]
        exemplar_count = sum(1 for item in items if item["category"] == "query_exemplar"
                             and not item["decayed"])
        if not semantic and not exemplar_count:
            return _memory_text(session_id, "我还没有记住您的偏好。您可以对我说「以后趋势都用折线图」。", ctype)
        lines = ["我当前记得："]
        for item in semantic[:10]:
            if item["category"] == "chart_preference":
                label = f"{item['family_name']} → {item['chart_name']}"
            else:
                term_id = item["value"].get("term_id", "")
                term_name = snapshot.terms.get(term_id, {}).get("name", term_id)
                label = f"「{item['memory_key']}」→「{term_name}」"
            state = "（待跨会话确认）" if item["status"] == "candidate" \
                else ("（已衰减，不生效）" if item["decayed"] else "")
            lines.append(f"{len(lines)}. {label}{state}")
        if exemplar_count:
            lines.append(f"另有 {exemplar_count} 条您已确认的个人问答对。")
        lines.append("可在右上角「记忆」中逐条删除，或对我说「忘掉全部记忆」。")
        return _memory_text(session_id, "\n".join(lines), ctype)
    if ctype == "forget_category":
        count = memory.clear_memories(user["id"], command["category"])
        label = "图表偏好" if command["category"] == "chart_preference" else "个人问答对"
        return _memory_text(session_id, f"已忘掉 {count} 条{label}记忆，历史审计记录保留。", ctype)
    if ctype == "forget_term":
        mention = command["mention"]
        targets = [item for item in memory.list_memories(user["id"])
                   if item["category"] == "term_mapping" and item["memory_key"] == mention]
        for item in targets:
            memory.delete_memory(user["id"], item["id"])
        ctx.setdefault("clarify_choices", {}).pop(f"term:{mention}", None)
        _persist_ctx(user["id"], session_id, ctx)
        return _memory_text(session_id, f"已忘掉 {len(targets)} 条「{mention}」口径记忆。", ctype)
    if ctype == "forget_all":
        count = memory.clear_memories(user["id"])
        ctx.update({"turns": [], "last_semql": None, "clarify_pending": None,
                    "clarify_choices": {}, "last_answer": None})
        _persist_ctx(user["id"], session_id, ctx)
        return _memory_text(
            session_id,
            f"已忘掉 {count} 条长期记忆，并清空当前会话上下文；历史消息与审计记录保留。",
            ctype)
    return _memory_text(session_id, "暂不支持该记忆操作。", "memory_error")


# ---------- 澄清选择的合并与记忆 ----------

def _merge_choice(amb_type: str, mention: str | None, selected_key: str,
                  intent: IntentResult, link_result) -> bool:
    """把澄清选项合并进 intent/link_result（clarify 续走与会话记忆沿用共用）。

    返回是否实际生效。
    """
    if amb_type == "TERM_CONFLICT" and selected_key:
        link_result.matched_elements = [
            m for m in link_result.matched_elements
            if not (m["element_type"] == "term"
                    and m["mention"] == mention
                    and m["element_id"] != selected_key)
        ]
        # 兜底：用户所选 term 若不在命中列表里（如仅来自 LLM 候选），补一条命中，
        # 确保下游 prompt 注入该术语口径、LLM 按所选指标出 SemQL
        if not any(m["element_type"] == "term" and m["element_id"] == selected_key
                   for m in link_result.matched_elements):
            link_result.matched_elements.append(
                {"element_type": "term", "element_id": selected_key,
                 "mention": mention or ""})
        return True
    if amb_type == "VALUE_CONFLICT" and selected_key:
        dim_id, _, tech = selected_key.partition("|")
        link_result.matched_elements = [
            m for m in link_result.matched_elements
            if not (m["element_type"] == "dim_value"
                    and m["mention"] == mention
                    and not (m["element_id"] == dim_id
                             and m.get("tech_value") == tech))
        ]
        return True
    if amb_type == "TIME_MISSING" and selected_key:
        try:
            s, e = resolve_time_range(selected_key, today())
            intent.time_range = {"start": s, "end": e}
            return True
        except ValueError:
            return False
    if amb_type == "GRAIN_MISSING" and selected_key:
        intent.grain = selected_key
        return True
    return False


def _apply_clarify_memory(ambiguities: list[dict], intent: IntentResult, link_result,
                          ctx: dict, snapshot, long_term: dict | None = None,
                          user_id: str | None = None,
                          session_id: str | None = None) -> tuple[list[dict], list[str]]:
    """依次沿用会话选择和长期口径偏好；候选已变化时失效并照常追问。"""
    choices = ctx.get("clarify_choices") or {}
    long_term = long_term or {}
    if (not choices and not long_term) or not ambiguities:
        return ambiguities, []
    remaining: list[dict] = []
    notes: list[str] = []
    for amb in ambiguities:
        atype, mention = amb["type"], amb.get("mention")
        selected = None
        label = None
        long_term_id = None
        if atype == "TERM_CONFLICT":
            cand = choices.get(f"term:{mention or ''}")
            if cand and cand in (amb.get("candidates") or []):
                selected = cand
                label = snapshot.terms.get(cand, {}).get("name", cand)
            else:
                # 会话选择优先；长期偏好也必须在本次候选中，避免语义层变更后强用旧口径
                preference = long_term.get(f"term:{mention or ''}")
                if preference and preference["selected_key"] in (amb.get("candidates") or []):
                    selected = preference["selected_key"]
                    label = preference["label"]
                    long_term_id = preference["memory_id"]
        elif atype == "VALUE_CONFLICT":
            cand = choices.get(f"value:{mention or ''}")
            if cand:
                dim_id, _, tech = cand.partition("|")
                hit = next((c for c in (amb.get("candidates") or [])
                            if c.startswith(f"{dim_id}:{tech}(")), None)
                if hit:
                    selected = cand
                    label = hit.partition("(")[2].rstrip(")") or tech
        if selected and _merge_choice(atype, mention, selected, intent, link_result):
            if long_term_id:
                notes.append(f"沿用您的长期口径偏好：「{mention}」按「{label}」查询")
                if user_id:
                    try:
                        memory.mark_accessed(long_term_id, user_id, session_id)
                    except Exception as exc:  # noqa: BLE001 - 统计失败不影响已生效的选择
                        logger.warning("长期记忆访问统计失败: %s", exc)
            else:
                notes.append(f"沿用本次会话的澄清选择：「{mention}」按「{label}」查询")
            continue
        remaining.append(amb)
    return remaining, notes


# ---------- 澄清挂起的续走 ----------

def clarify(user: dict, clarify_id: str, selected_key: str | None = None,
            free_text: str | None = None) -> Iterator[dict]:
    """澄清响应后续走查询链路（契约见 spec）。"""
    pending = None
    for sid, ctx in list(_CTX.items()):  # 快照遍历，避免并发 LRU 结构调整导致迭代异常
        p = ctx.get("clarify_pending")
        if p and p["clarify_id"] == clarify_id:
            pending = (sid, ctx, p)
            break
    if pending is None:
        yield _ev("error", {"code": "CLARIFY_EXPIRED",
                            "message": "澄清已过期，请重新提问", "retryable": False})
        return
    session_id, ctx, p = pending
    if p.get("user_id") != user["id"]:
        yield _ev("error", {"code": "FORBIDDEN", "message": "无权处理他人的澄清",
                            "retryable": False})
        return
    question = p["question"]

    if free_text:
        ctx["clarify_pending"] = None
        yield from ask(user, session_id, f"{question}（补充：{free_text}）")
        return
    if not selected_key:
        yield _ev("error", {"code": "CLARIFY_OPTION_REQUIRED",
                            "message": "请选择一个澄清选项", "retryable": False})
        return

    # 校验选项来自当前卡片，防止伪造 term/dimension 写入口径记忆
    candidates = p.get("candidates") or []
    if p["type"] == "VALUE_CONFLICT":
        dim_id, _, tech = selected_key.partition("|")
        valid = not candidates or any(c.startswith(f"{dim_id}:{tech}(") for c in candidates)
    else:
        valid = not candidates or selected_key in candidates
    intent: IntentResult = p["intent"]
    link_result = p["link_result"]
    merged = valid and _merge_choice(p["type"], p["mention"], selected_key, intent, link_result)
    if not valid or (p["type"] in ("TERM_CONFLICT", "VALUE_CONFLICT") and not merged):
        yield _ev("error", {"code": "CLARIFY_OPTION_INVALID",
                            "message": "该选项已失效，请重新提问", "retryable": False})
        return
    # TIME_MISSING 的 custom 等自由输入选项不合并，保留原有默认时间兜底行为

    memory_notes = []
    if p["type"] in ("TERM_CONFLICT", "VALUE_CONFLICT"):
        prefix = "term" if p["type"] == "TERM_CONFLICT" else "value"
        ctx.setdefault("clarify_choices", {})[f"{prefix}:{p['mention'] or ''}"] = selected_key
        if p["type"] == "TERM_CONFLICT":
            try:
                observed = memory.observe_term_choice(
                    user["id"], p["mention"] or "", selected_key, session_id)
                if observed["promoted"]:
                    memory_notes.append(
                        f"已根据您在 {observed['sessions']} 个会话中的一致选择，"
                        "将「" + (p["mention"] or "") + "」的口径记为长期偏好")
            except Exception as exc:  # noqa: BLE001 - 长期晋升失败不影响本次澄清
                logger.warning("澄清选择长期记忆写入失败: %s", exc)
    ctx["clarify_pending"] = None
    _persist_ctx(user["id"], session_id, ctx)
    yield from _run_data_query(user, session_id, question, intent, link_result,
                               ctx, clarified=True, pre_notes=memory_notes)


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

    ctx = _get_ctx(session_id, user["id"])
    snapshot = load_snapshot()

    # 记忆命令是严格匹配的独立入口，普通“用折线图展示……”仍按数据问题处理
    command = _parse_memory_command(question, snapshot)
    if command:
        yield _ev("intent", {"intent": "MEMORY", "mentions": [], "time_range": None,
                             "grain": None, "degraded": False, "assumptions": [],
                             "session_id": session_id, "question": question,
                             "rewritten": False, "inherits": False,
                             "memory_command": command["type"]})
        try:
            yield _handle_memory_command(user, session_id, ctx, command, snapshot)
        except Exception as exc:  # noqa: BLE001 - 记忆操作异常不应冲垮 SSE 通道
            logger.exception("记忆命令执行失败")
            yield _memory_text(session_id, f"记忆操作未完成：{exc}。数据查询功能不受影响。",
                               "memory_error")
        return

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

    # 歧义复核：当前会话选择优先，其次是仍有效的长期口径偏好
    ambiguities = detect_ambiguities(rewritten, intent, link_result, snapshot)
    try:
        long_term = memory.get_term_preferences(user["id"], snapshot)
    except Exception as exc:  # noqa: BLE001 - 长期记忆不可用时保留会话记忆和澄清卡
        logger.warning("长期口径偏好读取失败（已降级）: %s", exc)
        long_term = {}
    ambiguities, inherit_notes = _apply_clarify_memory(
        ambiguities, intent, link_result, ctx, snapshot, long_term,
        user_id=user["id"], session_id=session_id)
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
                "type": card.type, "user_id": user["id"],
                "mention": chosen_amb.get("mention"),
                "candidates": chosen_amb.get("candidates") or [],
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

    # 分派（inherit_notes 非空表示已按会话记忆改写 link_result，需透传给 run_query）
    if intent.intent in (Intent.INSIGHT_WHY, Intent.INSIGHT_ANOMALY_CHECK):
        yield from _run_insight(user, session_id, rewritten, intent, link_result, ctx,
                                pre_notes=inherit_notes)
    else:
        yield from _run_data_query(user, session_id, rewritten, intent, link_result, ctx,
                                   pre_notes=inherit_notes)


def _run_data_query(user: dict, session_id: str, question: str, intent: IntentResult,
                    link_result, ctx: dict, clarified: bool = False,
                    pre_notes: list[str] | None = None) -> Iterator[dict]:
    """DATA_QUERY 链路：generate→compile→gateway→execute→chart→summary。"""
    snapshot = load_snapshot()
    assumptions: list[str] = list(pre_notes or [])
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
                           # 澄清续走/沿用会话记忆：复用已按用户选择过滤的 link_result；
                           # 否则传 None 由 run_query 内部重新 link（保持原有行为）
                           link_result=link_result if (clarified or pre_notes) else None)
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
    try:
        chart, chart_notes = memory.personalize_chart(
            user["id"], chart, cols_meta, result["semql"], question, session_id)
    except Exception as exc:  # noqa: BLE001 - 图表偏好失败时使用原规则推荐
        logger.warning("图表偏好读取失败（已降级）: %s", exc)
        chart_notes = []
    assumptions.extend(chart_notes)
    memory_trace = list(pre_notes or []) + list(result.get("memory_trace") or []) + chart_notes
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
    ctx["last_answer"] = {"answer_id": answer_id,
                          "family": chart.get("family", "other")}
    ctx["turns"] = (ctx.get("turns") or [])[-4:] + [
        {"question": question, "intent": intent.intent.value,
         "semql_brief": _semql_brief(result["semql"])}]
    _persist_ctx(user["id"], session_id, ctx)

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
        "memory_trace": memory_trace,
        "semantic_version": result["semantic_version"],
        "clarified": clarified,
    })


def _run_insight(user: dict, session_id: str, question: str, intent: IntentResult,
                 link_result, ctx: dict,
                 pre_notes: list[str] | None = None) -> Iterator[dict]:
    """INSIGHT_WHY / INSIGHT_ANOMALY_CHECK 链路：fluctuation → attribution → narrative。"""
    snapshot = load_snapshot()
    policies = get_user_policies(user["id"])
    flat_policies = policies["row_policies"] + policies["column_policies"]
    metric_id = _pick_metric(snapshot, link_result)
    period_a, period_b, assumptions = _parse_why_periods(question, intent)
    assumptions = list(pre_notes or []) + assumptions
    base_filters = [{"dimension": "dim_channel", "op": "not_in", "values": ["TEST"]}]
    # 把问题中命中的维度值（如"华北区"→HB）并入基础过滤：
    # 否则波动识别/归因按全公司口径计算，与同会话内按维度过滤的查询结果互相矛盾
    dim_vals: dict[str, list] = {}
    for m in link_result.matched_elements:
        if m["element_type"] == "dim_value" and m.get("tech_value") is not None \
                and m["element_id"] in snapshot.dimensions:
            dim_vals.setdefault(m["element_id"], [])
            if m["tech_value"] not in dim_vals[m["element_id"]]:
                dim_vals[m["element_id"]].append(m["tech_value"])
    for did, vals in dim_vals.items():
        base_filters.append({"dimension": did, "op": "in", "values": vals})

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
                    f"历史均值 {fluc['mu']}%±{INSIGHT_SIGMA_K:g}σ，"
                    f"显著阈值 ±{INSIGHT_SIGNIFICANT_PCT:g}%）。")
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
                       f"较基期变化 {fluc['pct']}%，未达显著阈值"
                       f"（|环比|≥{INSIGHT_SIGNIFICANT_PCT:g}% 或偏离历史均值 {INSIGHT_SIGMA_K:g}σ），无需归因。",
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
    _persist_ctx(user["id"], session_id, ctx)

    yield _ev("result", {
        "answer_type": "insight", "question": question,
        "report_id": rid, "metric_id": metric_id,
        "report": report, "narrative": narrative,
        "narrative_degraded": degraded,
        "chart": {"type": "waterfall", "reason": "rule_11: 归因结果",
                  "available_types": ["waterfall", "table"]},
        "assumptions": assumptions,
        "memory_trace": list(pre_notes or []),
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
    _persist_ctx(user["id"], session_id, ctx)
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
