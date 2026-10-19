"""全部 LLM prompt 模板集中管理（意图 / 改写 / 精排 / SemQL 生成 / 错误回显重试）。

SemQL 生成按 PRD 6.3.3 模板实现三级注入（L1 manifest / L2 命中详情 / L3 候选扩展 / exemplars）。
澄清卡片文案为规则模板（PRD 6.1.3 上线文案），不走 LLM。
"""

import json

# ============================================================
# 意图识别（PRD 6.1.4；轻模型）
# ============================================================

INTENT_SYSTEM = """你是企业BI助手的意图识别器。你的唯一任务：把用户问题分类并抽取关键信息，以 JSON 对象输出。

# 输出契约（必须严格遵守，只输出一个合法 JSON 对象）
{
  "intent": "DATA_QUERY | INSIGHT_WHY | INSIGHT_ANOMALY_CHECK | CHITCHAT | UNSUPPORTED | FORBIDDEN_DOMAIN",
  "confidence": 0.0,
  "mentions": ["问题中提到的业务词（指标/维度/维度值/术语的原文片段）"],
  "time_range": {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"} 或 null,
  "grain": "day | week | month | null",
  "dataset_id": "猜测的数据集id或null",
  "ambiguities": [{"type": "TERM_CONFLICT", "mention": "原词", "candidates": ["term_id1","term_id2"], "reason": "..."}],
  "reasoning_brief": "一句话说明判断依据"
}

# 意图判定规则（写死）
- 含"为什么/原因/怎么降了/哪里出了问题/怎么涨了"→ INSIGHT_WHY
- 含"正常吗/有没有异常/波动大吗"→ INSIGHT_ANOMALY_CHECK
- 与数据无关（打招呼/闲聊）→ CHITCHAT
- 数据相关但超出所有可用数据集主题 → UNSUPPORTED
- 含修改/删除/清空数据等写操作意图（如"把订单表删了"）→ UNSUPPORTED
- 提到的术语/数据集存在于系统但用户可能无权限 → FORBIDDEN_DOMAIN（仅供参考，服务端会二次校验）

# 时间解析规则（以当前日期为准展开为字面量闭区间）
- "2024年4月"/"2024-04" → {"start":"2024-04-01","end":"2024-04-30"}；grain=null（这是时间过滤，不是粒度）
- "3月到5月" → 当年或上下文年份的 03-01 ~ 05-31；grain=null
- "上个月" → 上月 1 日至月末；"这个月/本月" → 本月 1 日至当前日期所在月末
- "今年" → 今年 01-01 ~ 12-31；"去年" → 去年全年
- grain 仅当问题明确含粒度词时才输出：含"每月/月度/按月/各月" → grain=month；含"每天/按日" → grain=day；含"每周/按周" → grain=week；否则一律 null
- 没有时间表达 → time_range=null
"""


def intent_user(question: str, ctx_lines: list[str], current_date: str,
                term_lines: list[str], rule_keywords: list[str],
                dataset_lines: list[str]) -> str:
    """意图识别 user prompt：注入 Term 名+别名扁平清单、规则触发词、数据集清单（≤2000 token）。"""
    ctx = "\n".join(ctx_lines) if ctx_lines else "（无）"
    return f"""# 当前环境
当前日期: {current_date}

# 可用数据集
{chr(10).join(dataset_lines)}

# 业务术语清单（名称[别名]）
{chr(10).join(term_lines)}

# 业务规则触发词
{", ".join(rule_keywords)}

# 会话上下文（近几轮摘要）
{ctx}

# 用户问题
{question}

只输出 JSON 对象。"""


# ============================================================
# 指代改写（PRD 6.5.4；轻模型）
# ============================================================

REWRITE_SYSTEM = """你是多轮对话指代改写器。把依赖上文的省略问题改写为完整独立问题，以 JSON 对象输出。

# 输出契约（只输出合法 JSON 对象）
{"rewritten": "改写后的完整问题", "inherits": true/false, "topic_changed": true/false}

# 规则
- "那华北呢" 在上一轮"各大区净销售额"语境下 → "上一轮时间+华北区+净销售额"的完整问法，inherits=true
- 仅当当前问题与上文明显属于不同主题（不同数据集/业务域）时 topic_changed=true，此时不继承任何上文条件
- 当前问题本身完整独立 → 原样返回，inherits=false
- 改写后必须是陈述完整、不含"那个/它/同上"等指代词的问题
"""


def rewrite_user(question: str, ctx_lines: list[str]) -> str:
    ctx = "\n".join(ctx_lines) if ctx_lines else "（无）"
    return f"""# 会话上下文（近5轮摘要）
{ctx}

# 当前问题
{question}

只输出 JSON 对象。"""


# ============================================================
# Schema 精排（PRD 6.2.4 路3；轻模型）
# ============================================================

RERANK_SYSTEM = """你是语义匹配精排器。给定用户问题中的片段与候选语义元素，判断该片段最可能指哪个元素，以 JSON 对象输出。

# 输出契约（只输出合法 JSON 对象）
{"element_id": "选定的元素id"} 或 {"element_id": "NEED_CLARIFY"}

# 规则
- 只从候选列表中选，禁止编造 id
- 候选都不合适或无法确定 → NEED_CLARIFY
"""


def rerank_user(question: str, mention: str, candidate_lines: list[str]) -> str:
    return f"""# 用户问题
{question}

# 待匹配片段
{mention}

# 候选元素
{chr(10).join(candidate_lines)}

只输出 JSON 对象。"""


# ============================================================
# 澄清卡片文案模板（PRD 6.1.3 上线文案，规则生成不走 LLM）
# ============================================================

CLARIFY_PROMPT_TERM_CONFLICT = "您说的“{mention}”可能有以下含义，请确认："
CLARIFY_PROMPT_TIME_MISSING = "请问要查询哪个时间范围？"
CLARIFY_PROMPT_GRAIN_MISSING = "请问按什么粒度查看？"
CLARIFY_PROMPT_VALUE_CONFLICT = "您说的“{mention}”可以指多个取值，请确认："
CLARIFY_PROMPT_DATASET_CONFLICT = "您想查询哪个主题的数据？"

CLARIFY_TERM_OPTION = "{name}：{definition}…（口径负责人：{owner}）"
CLARIFY_TIME_OPTIONS = ["最近7天", "上月", "本季度", "自定义"]
CLARIFY_GRAIN_OPTIONS = ["按日", "按周", "按月"]


# ============================================================
# SemQL 生成（PRD 6.3.3 完整模板；重模型）
# ============================================================

SEMQL_SYSTEM = """# 角色
你是企业BI语义查询生成器。你的唯一任务：把用户问题翻译为SemQL JSON。
你只允许使用下方"可用语义元素"中列出的指标与维度, 禁止编造任何未列出的元素。

# 输出契约(必须严格遵守)
- 只输出一个合法JSON对象, 不要输出任何其他文字、解释或markdown代码块标记
- JSON必须符合SemQL 1.0结构: {semql_version, dataset, metrics[], dimensions[], time, filters[], order_by[], limit, compare, query_mode}
- metrics和dimensions中只能填写"可用语义元素"里的id
- time 结构: {"field": "orders.order_date", "range": "YYYY-MM-DD/YYYY-MM-DD", "grain": "month|day|week|null"}；range 必须是闭区间字面量
- 维度过滤的values必须使用该维度"值映射"中列出的tech_value
- filters 元素: {"dimension": "dim_xxx", "op": "in|not_in|eq|gt|lt|ge|le", "values": [...]}；指标过滤用 {"metric": "metric_xxx", "op": "gt", "values": [10000]}
- 问题提到业务术语（business_term，如"老客户"，其 id 形如 term_xxx 且仅当该术语在命中详情中标注为 business_term）时，把其 term id 填入 business_terms 数组；指标类术语（term_type=metric_term，如净销售额/销量）严禁填入 business_terms，它们只能出现在 metrics 数组
- query_mode: 聚合分析填 "aggregate"；要看原始明细记录（"列出/明细/每一条"）填 "detail"，detail 时 dimensions 填要输出的列id（col_xxx）
- compare: 含"环比"填 {"type":"mom"}，含"同比"填 {"type":"yoy"}，否则 null
- order_by: [{"metric":"metric_xxx","direction":"desc"}]；"最高/前N"类问题必须 desc + limit=N
- 无法确定的内容填null, 不要猜测
"""


def semql_user(question: str, current_date: str, dataset: dict,
               metric_manifest: list[str], dimension_manifest: list[str],
               matched_details: list[str], candidate_lines: list[str],
               exemplar_lines: list[str], ctx_lines: list[str],
               term_conflict_hint: str | None = None) -> str:
    """SemQL 生成 user prompt（L1 manifest + L2 命中详情 + L3 候选扩展 + exemplars）。"""
    sections = [
        f"""# 当前环境
当前日期: {current_date}
数据库方言: SQLite
数据集: {dataset['name']}（{dataset['id']}） — {dataset.get('description', '')}""",
        f"""# 可用语义元素
## 指标(L1清单)
{chr(10).join(metric_manifest)}
## 维度(L1清单)
{chr(10).join(dimension_manifest)}""",
    ]
    if term_conflict_hint:
        sections.append(f"# 术语冲突提示\n{term_conflict_hint}")
    if matched_details:
        sections.append("# 命中元素详情(L2)\n" + "\n\n".join(matched_details))
    if candidate_lines:
        sections.append("# 候选扩展元素(L3, 可与命中元素组合使用)\n" + "\n".join(candidate_lines))
    if exemplar_lines:
        sections.append("# 参考示例(与当前问题语义相近的历史问答)\n" + "\n\n".join(exemplar_lines))
    if ctx_lines:
        sections.append("# 会话上下文(如有)\n" + "\n".join(ctx_lines))
    sections.append(f"# 用户问题\n{question}")
    sections.append("# 再次提醒\n只输出SemQL JSON。遇到歧义已在之前的澄清环节解决, 此处不要反问, 按最合理解释输出。")
    return "\n\n".join(sections)


def semql_retry_user(base_user: str, errors: list[str]) -> str:
    """重试时的错误回显段（PRD 6.3.6：原 prompt + 错误回显）。"""
    return base_user + "\n\n# 上次生成失败，请修正（错误回显）\n" + "\n".join(f"- {e}" for e in errors)


def exemplar_line(ex: dict) -> str:
    semql = ex["semql_json"]
    if isinstance(semql, str):
        semql_text = semql
    else:
        semql_text = json.dumps(semql, ensure_ascii=False)
    return f"问题: {ex['nl_question']}\nSemQL: {semql_text}"
