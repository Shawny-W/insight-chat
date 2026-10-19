"""归因结论组织：LLM（heavy）按 PRD 6.6.4 Step5 模板写作；失败用规则模板拼接。

narrate(attribution_dict) -> (text, degraded)
"""

import json
import logging

from chatbi.llm import chat_text

logger = logging.getLogger(__name__)

# PRD 6.6.4 Step5 系统侧完整模板
NARRATIVE_SYSTEM = """# 角色
你是BI洞察报告撰写人。你只能基于下方"归因事实JSON"写作, 禁止使用JSON之外的任何数据,
禁止推测JSON中没有的原因。写作对象是业务人员, 语言直白, 避免统计学术语。

# 写作结构(严格遵守)
1. 结论(≤2句): 什么指标、在什么期间、变化多少、是否显著
2. 主要原因(≤3条): 按贡献度降序, 每条格式"维度值+变化量+占总变化百分比", 如下钻有第二层则补一句细分
3. 业务事件佐证(若有events): "时间上重合的业务事件: ...", 必须加"仅供参考, 不代表因果关系"
4. 低基数提示(若有low_base_warnings)
5. 建议下一步(1条): 从归因路径推荐一个可继续追问的问题

# 输出
直接输出报告正文, 不要标题, 不要复述JSON, 总字数≤250字。
"""


def narrate(attribution: dict) -> tuple[str, bool]:
    """attribution_json → 自然语言报告；LLM 失败降级为规则模板，返回 (text, degraded)。"""
    try:
        text = chat_text(
            "insight_narrative", NARRATIVE_SYSTEM,
            "# 归因事实JSON\n" + json.dumps(attribution, ensure_ascii=False, indent=2),
            heavy=True, temperature=0.3,
        )
        if text.strip():
            return text.strip(), False
    except Exception as exc:  # noqa: BLE001 - LLM 结论失败降级为模板拼接
        logger.warning("归因结论 LLM 生成失败，降级为模板拼接: %s", exc)
    return _template_narrative(attribution), True


def _fmt_num(v: float | int | None, unit: str) -> str:
    if v is None:
        return "未知"
    if isinstance(v, float) and not v.is_integer():
        return f"{v:,.2f}{unit}"
    return f"{int(v):,}{unit}"


def _template_narrative(a: dict) -> str:
    """规则模板拼接（结论 + top3 原因 + 事件佐证 + 低基数提示 + 建议）。"""
    metric = a.get("metric", {})
    name = metric.get("name", "指标")
    unit = metric.get("unit") or ""
    pa, pb = a.get("period", {}).get("A", "?"), a.get("period", {}).get("B", "?")
    values = a.get("values", {})
    v_a, v_b, pct = values.get("V_A"), values.get("V_B"), values.get("pct")

    direction = "下降" if (values.get("delta") or 0) < 0 else "上升"
    lines = [f"{name}在{pa}为{_fmt_num(v_a, unit)}，较{pb}{direction}"
             f"{abs(pct):.1f}%（变化显著）。"
             if pct is not None else f"{name}在{pa}为{_fmt_num(v_a, unit)}。"]

    reasons = []
    for node in a.get("drill_tree", []):
        dim_name = node.get("dimension", "")
        for t in node.get("top", [])[:3]:
            seg = f"{dim_name}「{t['value']}」变化 {_fmt_num(t['contrib'], unit)}" \
                  f"（占该层 {abs(t['contrib_pct']):.1f}%）"
            reasons.append(seg)
    if reasons:
        lines.append("主要原因：" + "；".join(reasons[:3]) + "。")
    elif a.get("diffuse"):
        lines.append("变化较为分散，未发现能解释主要波动的单一维度因素。")

    events = a.get("events") or []
    if events:
        ev = "、".join(f"{e['name']}（{e['start_date']}~{e['end_date']}）" for e in events)
        lines.append(f"时间上重合的业务事件：{ev}，仅供参考，不代表因果关系。")

    for w in a.get("low_base_warnings") or []:
        lines.append(f"低基数提示：{w}")

    if a.get("drill_tree"):
        top = a["drill_tree"][0]
        if top.get("top"):
            t0 = top["top"][0]
            lines.append(f"建议继续追问：{t0['value']}的详细构成。")
    return "\n".join(lines)
