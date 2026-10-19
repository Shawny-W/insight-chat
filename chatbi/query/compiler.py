"""SemQL → SQLite 物理 SQL 编译器（纯确定性代码，无 LLM）。

要点（契约见 spec 第 6 节）：
- atomic metric 聚合，filter 用 SUM(CASE WHEN ...) / COUNT(DISTINCT CASE WHEN ...)（SQLite 无 FILTER）
- derived metric 沿 formula 展开（当前支持 X / Y 形式）
- join 路径：Relationship 图 BFS；many_to_many 报 COMPILE_FANTRAP_RISK；无路径报 COMPILE_NO_JOIN_PATH
- business_term rule_sql 内联（{t_start} 用 time.start 代入）
- block 规则强制注入并记入 applied_rules
- compare mom/yoy：有 grain 用 LAG 窗口；无 grain 用双期 CASE WHEN 列
"""

import re
from dataclasses import dataclass, field
from datetime import date

from chatbi.query.semql import SemQL, resolve_time_range, today, translate_dim_value
from chatbi.semantic.store import SemanticSnapshot


class CompileError(Exception):
    """编译失败；code 供重试链分流（可修复/结构性）。"""

    def __init__(self, code: str, message: str):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


@dataclass
class CompileResult:
    sql: str
    applied_rules: list[str] = field(default_factory=list)
    trace: dict = field(default_factory=dict)


# ---------- 工具 ----------

def _q(s: str) -> str:
    """字符串字面量转义。"""
    return "'" + str(s).replace("'", "''") + "'"


def _shift_months(d: date, months: int) -> date:
    """日期按月份平移（compare 基期用）。"""
    total = d.year * 12 + (d.month - 1) + months
    y, m = total // 12, total % 12 + 1
    import calendar
    day = min(d.day, calendar.monthrange(y, m)[1])
    return date(y, m, day)


def _grain_expr(time_field: str, grain: str) -> tuple[str, str]:
    """grain → (分组表达式, 列别名)。"""
    if grain == "month":
        return f"strftime('%Y-%m', {time_field})", "月份"
    if grain == "week":
        return f"strftime('%Y-W%W', {time_field})", "周"
    return f"date({time_field})", "日期"


# ---------- 指标展开 ----------

def _metric_expr(m: dict, extra_cond: str | None = None) -> str:
    """atomic metric → 聚合 SQL 表达式；extra_cond 叠加进 CASE WHEN（compare 双期用）。"""
    agg = m["agg"]
    expr = m["measure_expression"]
    filt = m.get("filter_sql")
    conds = [c for c in (filt, extra_cond) if c]
    cond = " AND ".join(f"({c})" for c in conds)
    if agg == "COUNT_DISTINCT":
        inner = f"CASE WHEN {cond} THEN {expr} ELSE NULL END" if cond else expr
        return f"COUNT(DISTINCT {inner})"
    if cond:
        return f"{agg}(CASE WHEN {cond} THEN {expr} ELSE 0 END)"
    return f"{agg}({expr})"


def _expand_metric(metric_id: str, snapshot: SemanticSnapshot,
                   extra_cond: str | None = None) -> str:
    """展开指标为 SQL 表达式（derived 沿 formula 递归，当前支持 X / Y）。"""
    m = snapshot.metrics[metric_id]
    if m["metric_kind"] == "atomic":
        return _metric_expr(m, extra_cond=extra_cond)
    parts = [p.strip() for p in (m.get("formula") or "").split("/")]
    if len(parts) != 2 or any(p not in snapshot.metrics for p in parts):
        raise CompileError("COMPILE_UNSUPPORTED_FORMULA",
                           f"派生指标 {metric_id} 的公式仅支持 指标A / 指标B 形式: {m.get('formula')}")
    num = _expand_metric(parts[0], snapshot, extra_cond)
    den = _expand_metric(parts[1], snapshot, extra_cond)
    return f"ROUND(({num}) * 1.0 / NULLIF(({den}), 0), 2)"


def _metric_models(metric_id: str, snapshot: SemanticSnapshot) -> set[str]:
    """指标涉及的模型集合（derived 递归）。"""
    m = snapshot.metrics[metric_id]
    if m["metric_kind"] == "atomic":
        models = {m["model_id"]}
        for rel_id in m.get("joins_needed", []):
            rel = next((r for r in snapshot.relationships if r["id"] == rel_id), None)
            if rel:
                models.update([rel["from_model_id"], rel["to_model_id"]])
        return models
    models: set[str] = set()
    for ref in re.findall(r"metric_[a-z0-9_]+", m.get("formula") or ""):
        if ref in snapshot.metrics:
            models |= _metric_models(ref, snapshot)
    return models


# ---------- join 路径推导（BFS） ----------

def _find_joins(snapshot: SemanticSnapshot, main_model: str,
                needed_models: set[str]) -> list[dict]:
    """从主模型 BFS 到全部涉及模型，回溯收集路径上的边（按主模型向外顺序，去重）。"""
    needed = needed_models - {main_model}
    if not needed:
        return []
    adj: dict[str, list[tuple[str, dict]]] = {}
    for r in snapshot.relationships:
        adj.setdefault(r["from_model_id"], []).append((r["to_model_id"], r))
        adj.setdefault(r["to_model_id"], []).append((r["from_model_id"], r))

    # BFS 记录父指针
    parent: dict[str, tuple[str, dict]] = {}
    visited = {main_model}
    queue = [main_model]
    while queue:
        cur = queue.pop(0)
        for nxt, rel in adj.get(cur, []):
            if nxt in visited:
                continue
            visited.add(nxt)
            parent[nxt] = (cur, rel)
            queue.append(nxt)

    joins: list[dict] = []
    seen: set[str] = set()
    for target in sorted(needed):
        if target not in parent:
            raise CompileError("COMPILE_NO_JOIN_PATH",
                               f"模型 {target} 与主模型 {main_model} 之间无可达 join 路径")
        chain: list[dict] = []
        cur = target
        while cur != main_model:
            prev, rel = parent[cur]
            if rel["join_type"] == "many_to_many":
                raise CompileError(
                    "COMPILE_FANTRAP_RISK",
                    f"关系 {rel['id']}（{rel['from_model_id']}↔{rel['to_model_id']}）为 many_to_many，"
                    f"存在扇形膨胀风险，已拒绝；请检查模型")
            chain.append(rel)
            cur = prev
        for rel in reversed(chain):  # 主模型向外的顺序
            if rel["id"] not in seen:
                seen.add(rel["id"])
                joins.append(rel)
    return joins


# ---------- 主编译 ----------

def compile(semql: SemQL, snapshot: SemanticSnapshot) -> CompileResult:  # noqa: C901
    """compile(semql, snapshot) -> CompileResult（契约签名固定）。"""
    trace: dict = {"metrics": {}, "dimensions": {}, "filters": [], "joins": [],
                   "business_terms": [], "rules": []}
    applied_rules: list[str] = []

    ds = snapshot.datasets.get(semql.dataset)
    if ds is None:
        raise CompileError("SEMQL_INVALID_REF", f"数据集 {semql.dataset} 不存在")

    # ---- 主模型 ----
    if semql.metrics:
        first = snapshot.metrics.get(semql.metrics[0])
        if first is None:
            raise CompileError("SEMQL_INVALID_REF", f"指标 {semql.metrics[0]} 不存在")
        if first["metric_kind"] == "atomic":
            main_model = first["model_id"]
        else:
            main_model = sorted(_metric_models(first["id"], snapshot))[0]
    elif semql.dimensions:
        first_dim = semql.dimensions[0]
        if first_dim in snapshot.dimensions:
            main_model = snapshot.dimensions[first_dim]["model_id"]
        else:
            col = next((c for c in snapshot.columns if c["id"] == first_dim), None)
            main_model = col["model_id"] if col else ds["model_ids"][0]
    else:
        raise CompileError("SEMQL_INVALID_REF", "SemQL 既无指标也无维度/列")

    # ---- 涉及模型集合与 join ----
    needed_models: set[str] = set()
    for mid in semql.metrics:
        m = snapshot.metrics.get(mid)
        if m is None:
            raise CompileError("SEMQL_INVALID_REF", f"指标 {mid} 不存在")
        needed_models |= _metric_models(mid, snapshot)
    for did in semql.dimensions:
        if did in snapshot.dimensions:
            needed_models.add(snapshot.dimensions[did]["model_id"])
        else:
            col = next((c for c in snapshot.columns if c["id"] == did), None)
            if col is None:
                raise CompileError("SEMQL_INVALID_REF", f"维度/列 {did} 不存在")
            needed_models.add(col["model_id"])
    for f in semql.filters:
        if f.dimension:
            dim = snapshot.dimensions.get(f.dimension)
            if dim is None:
                raise CompileError("SEMQL_INVALID_REF", f"过滤维度 {f.dimension} 不存在")
            needed_models.add(dim["model_id"])
    if semql.business_terms:
        needed_models.add("mdl_customers")  # 老客户 rule_sql 落在 orders/customers

    joins = _find_joins(snapshot, main_model, needed_models)
    for j in joins:
        trace["joins"].append({"id": j["id"], "condition": j["condition"],
                               "join_type": j["join_type"]})

    # ---- 时间 ----
    time_field = None
    time_start = time_end = None
    grain = None
    if semql.time is not None:
        time_start, time_end = resolve_time_range(semql.time.range, today())
        grain = semql.time.grain
        time_field = semql.time.field
    if time_field is None:
        if semql.metrics:
            first = snapshot.metrics[semql.metrics[0]]
            time_field = first.get("default_time_field")
        if time_field is None:
            time_field = snapshot.models[main_model].get("time_field")
    trace["time"] = {"field": time_field, "start": time_start, "end": time_end,
                     "grain": grain}

    # 时间字段所在表必须纳入 join（如 metric 在 order_items 而 time_field 在 orders）
    if time_field and "." in time_field:
        tf_table = time_field.split(".", 1)[0]
        tf_model = next((m for m in snapshot.models.values()
                         if m.get("table_ref") == tf_table), None)
        if tf_model is not None:
            needed_models.add(tf_model["id"])
            joins = _find_joins(snapshot, main_model, needed_models)
            trace["joins"] = [{"id": j["id"], "condition": j["condition"],
                               "join_type": j["join_type"]} for j in joins]

    # ---- SELECT 列 ----
    select_cols: list[str] = []
    group_exprs: list[str] = []

    if semql.query_mode == "detail":
        for did in semql.dimensions:
            if did in snapshot.dimensions:
                dim = snapshot.dimensions[did]
                select_cols.append(f"{dim['expression']} AS \"{dim['display_name']}\"")
                trace["dimensions"][did] = {"expr": dim["expression"],
                                            "alias": dim["display_name"]}
            else:
                col = next(c for c in snapshot.columns if c["id"] == did)
                table = snapshot.models[col["model_id"]]["table_ref"]
                expr = col.get("expression") or f"{table}.{col['name']}"
                select_cols.append(f"{expr} AS \"{col['display_name']}\"")
                trace["dimensions"][did] = {"expr": expr, "alias": col["display_name"]}
    else:
        if grain and time_field:
            gexpr, galias = _grain_expr(time_field, grain)
            select_cols.append(f"{gexpr} AS \"{galias}\"")
            group_exprs.append(gexpr)
        for did in semql.dimensions:
            dim = snapshot.dimensions.get(did)
            if dim is None:
                raise CompileError("SEMQL_INVALID_REF",
                                   f"aggregate 模式不支持输出列 {did}（仅 detail 可用 col_*）")
            select_cols.append(f"{dim['expression']} AS \"{dim['display_name']}\"")
            group_exprs.append(dim["expression"])
            trace["dimensions"][did] = {"expr": dim["expression"],
                                        "alias": dim["display_name"]}
        for mid in semql.metrics:
            term = snapshot.term_of_metric(mid)
            alias = term["name"] if term else mid
            expr = _expand_metric(mid, snapshot)
            select_cols.append(f"{expr} AS \"{alias}\"")
            trace["metrics"][mid] = {"expr": expr, "alias": alias}

    # ---- WHERE 条件 ----
    main_table = snapshot.models[main_model]["table_ref"]
    where: list[str] = []
    if time_field and time_start and time_end:
        where.append(f"{time_field} BETWEEN {_q(time_start)} AND {_q(time_end)}")

    has_channel_filter = any(f.dimension == "dim_channel" for f in semql.filters)
    for f in semql.filters:
        if f.dimension:
            dim = snapshot.dimensions[f.dimension]
            expr = dim["expression"]
            values = []
            for v in f.values:
                tech = translate_dim_value(dim, v)
                if tech is None:
                    raise CompileError(
                        "SEMQL_INVALID_VALUE",
                        f"维度 {f.dimension}（{dim['display_name']}）的值 {v!r} 无法翻译为 tech_value")
                values.append(tech)
            lit = ", ".join(_q(v) for v in values)
            if f.op == "in":
                cond = f"{expr} IN ({lit})"
            elif f.op == "not_in":
                cond = f"{expr} NOT IN ({lit})"
            elif f.op == "eq":
                cond = f"{expr} = {lit}"
            else:
                op_sql = {"gt": ">", "lt": "<", "ge": ">=", "le": "<="}[f.op]
                cond = f"{expr} {op_sql} {lit}"
            where.append(cond)
            trace["filters"].append({"dimension": f.dimension, "op": f.op,
                                     "values": values, "sql": cond})
        elif f.metric:
            m = snapshot.metrics.get(f.metric)
            if m is None:
                raise CompileError("SEMQL_INVALID_REF", f"过滤指标 {f.metric} 不存在")
            # 指标过滤 → HAVING（聚合后）
            pass  # 在 HAVING 阶段处理（见下）

    # business_term 内联（rule_sql 模板，{t_start} 代入时间起点）
    for tid in semql.business_terms:
        mapping = next((m for m in snapshot.mappings
                        if m["mapping_type"] == "term_rule_sql" and m["ont_id"] == tid), None)
        if mapping is None:
            raise CompileError("SEMQL_INVALID_REF", f"业务术语 {tid} 缺少 rule_sql 映射")
        t_start = time_start or today().isoformat()
        rule_sql = mapping["mdl_id"].replace("{t_start}", t_start)
        where.append(f"({rule_sql})")
        term = snapshot.terms.get(tid, {})
        trace["business_terms"].append({"term": tid, "sql": rule_sql})
        applied_rules.append(f"业务术语[{term.get('name', tid)}]已内联过滤条件（首单满一年）")

    # block 规则强制：排除测试渠道（查询实际涉及 orders 表且未显式过滤渠道时）
    join_tables: set[str] = {main_table}
    for j in joins:
        for side in (j["from_model_id"], j["to_model_id"]):
            if side in snapshot.models:
                join_tables.add(snapshot.models[side]["table_ref"])
    if "orders" in join_tables and not has_channel_filter:
        where.append("orders.channel != 'TEST'")
        applied_rules.append("规则[rule_exclude_test]: 已默认排除测试渠道（channel='TEST'）")

    # block 规则口径说明：含自带 completed 过滤的指标时记录
    completed_metrics = [mid for mid in semql.metrics
                         if "completed" in (snapshot.metrics[mid].get("filter_sql") or "")]
    if completed_metrics:
        applied_rules.append(
            "规则[rule_completed]: 净销售额/销量/有效订单数仅统计 status='completed' "
            "的订单（已在度量口径内生效）")

    # ---- FROM / JOIN ----
    from_sql = main_table
    joined = {main_table}
    for j in joins:
        other_model = (j["to_model_id"] if snapshot.models[j["from_model_id"]]["table_ref"] in joined
                       else j["from_model_id"])
        other_table = snapshot.models[other_model]["table_ref"]
        if other_table in joined:
            continue
        from_sql += f"\nJOIN {other_table} ON {j['condition']}"
        joined.add(other_table)

    # ---- HAVING（指标过滤） ----
    having: list[str] = []
    for f in semql.filters:
        if f.metric and f.op in ("gt", "lt", "ge", "le", "eq"):
            m = snapshot.metrics[f.metric]
            term = snapshot.term_of_metric(f.metric)
            alias = term["name"] if term else f.metric
            op_sql = {"gt": ">", "lt": "<", "ge": ">=", "le": "<=", "eq": "="}[f.op]
            having.append(f"\"{alias}\" {op_sql} {f.values[0]}")
            trace["filters"].append({"metric": f.metric, "op": f.op,
                                     "values": f.values, "sql": having[-1]})

    # ---- 组装内层 SQL ----
    sql = "SELECT " + ",\n       ".join(select_cols)
    sql += f"\nFROM {from_sql}"
    if where:
        sql += "\nWHERE " + "\n  AND ".join(where)
    if group_exprs:
        sql += "\nGROUP BY " + ", ".join(group_exprs)
    if having:
        sql += "\nHAVING " + " AND ".join(having)

    # ---- ORDER BY / LIMIT ----
    order_cols: list[str] = []
    for ob in semql.order_by:
        if ob.metric:
            m = snapshot.metrics.get(ob.metric)
            if m is None:
                raise CompileError("SEMQL_INVALID_REF", f"排序指标 {ob.metric} 不存在")
            term = snapshot.term_of_metric(ob.metric)
            order_cols.append(f"\"{term['name'] if term else ob.metric}\" {ob.direction.upper()}")
        elif ob.dimension:
            dim = snapshot.dimensions.get(ob.dimension)
            if dim is None:
                raise CompileError("SEMQL_INVALID_REF", f"排序维度 {ob.dimension} 不存在")
            order_cols.append(f"\"{dim['display_name']}\" {ob.direction.upper()}")
    if not order_cols and grain:
        galias = _grain_expr(time_field, grain)[1]
        order_cols.append(f"\"{galias}\" ASC")
    if order_cols:
        sql += "\nORDER BY " + ", ".join(order_cols)

    limit = semql.limit or 100
    if semql.query_mode == "detail":
        limit = min(limit, 1000)
    sql += f"\nLIMIT {limit}"
    trace["limit"] = limit
    trace["query_mode"] = semql.query_mode

    # ---- compare（环比/同比） ----
    if semql.compare is not None and semql.metrics:
        metric_id = semql.metrics[0]
        term = snapshot.term_of_metric(metric_id)
        alias = term["name"] if term else metric_id
        ctype = semql.compare.type
        if grain:
            if ctype == "period_vs_period":
                raise CompileError("COMPILE_UNSUPPORTED_COMPARE",
                                   "period_vs_period 不支持 grain 分组（归因分组请用无粒度模式）")
            # 有粒度：LAG 窗口（mom=前1桶；yoy=month 前12桶 / week 前52桶 / day 前365桶）
            offset = 1 if ctype == "mom" else {"month": 12, "week": 52, "day": 365}[grain]
            galias = _grain_expr(time_field, grain)[1]
            partition = ""
            if semql.dimensions:
                dim_aliases = ", ".join(
                    f"\"{snapshot.dimensions[d]['display_name']}\"" for d in semql.dimensions)
                partition = f"PARTITION BY {dim_aliases} "
            lag = (f"LAG(\"{alias}\", {offset}) OVER ({partition}ORDER BY \"{galias}\")")
            sql = (
                f"SELECT t.*,\n"
                f"       {lag} AS \"{alias}_基期\",\n"
                f"       \"{alias}\" - {lag} AS \"{alias}_差值\",\n"
                f"       ROUND((\"{alias}\" - {lag}) * 100.0 / NULLIF({lag}, 0), 1) AS \"{alias}_{'环比' if ctype == 'mom' else '同比'}%\"\n"
                f"FROM (\n{_indent(sql)}\n) t"
            )
        else:
            # 无粒度：整体两期对比（mom/yoy 基期=time 平移；period_vs_period 基期=显式 period_b）
            s, e = date.fromisoformat(time_start), date.fromisoformat(time_end)
            if ctype == "mom":
                ps, pe = _shift_months(s, -1), _shift_months(e, -1)
            elif ctype == "yoy":
                ps, pe = _shift_months(s, -12), _shift_months(e, -12)
            else:  # period_vs_period
                if not semql.compare.period_b:
                    raise CompileError("SEMQL_INVALID_REF",
                                       "compare.period_vs_period 需要 period_b 闭区间")
                pb_s, pb_e = resolve_time_range(semql.compare.period_b, today())
                ps, pe = date.fromisoformat(pb_s), date.fromisoformat(pb_e)
            cur_cond = f"{time_field} BETWEEN {_q(time_start)} AND {_q(time_end)}"
            prev_cond = f"{time_field} BETWEEN {_q(ps.isoformat())} AND {_q(pe.isoformat())}"
            cur_expr = _expand_metric(metric_id, snapshot, extra_cond=cur_cond)
            prev_expr = _expand_metric(metric_id, snapshot, extra_cond=prev_cond)
            metric_aliases = {trace["metrics"][mid]["alias"] for mid in trace["metrics"]}
            inner_selects = [
                c for c in select_cols
                if not any(c.rstrip().endswith(f"AS \"{a}\"") for a in metric_aliases)
            ]
            inner = (
                f"SELECT " + ",\n       ".join(inner_selects + [
                    f"{cur_expr} AS \"{alias}\"",
                    f"{prev_expr} AS \"{alias}_基期\"",
                ]) +
                f"\nFROM {from_sql}"
                f"\nWHERE ({cur_cond} OR {prev_cond})"
                + ("\n  AND " + "\n  AND ".join(
                    w for w in where if not w.startswith(f"{time_field} BETWEEN")) if len(where) > 1 else "")
            )
            if group_exprs:
                inner += "\nGROUP BY " + ", ".join(group_exprs)
            label = {"mom": "环比", "yoy": "同比"}.get(ctype, "对比")
            sql = (
                f"SELECT t.*,\n"
                f"       \"{alias}\" - \"{alias}_基期\" AS \"{alias}_差值\",\n"
                f"       ROUND((\"{alias}\" - \"{alias}_基期\") * 100.0 / NULLIF(\"{alias}_基期\", 0), 1) AS \"{alias}_{label}%\"\n"
                f"FROM (\n{_indent(inner)}\n) t"
            )
            if order_cols:
                sql += "\nORDER BY " + ", ".join(order_cols)
        trace["compare"] = {"type": ctype}

    return CompileResult(sql=sql, applied_rules=applied_rules, trace=trace)


def _indent(sql: str, pad: str = "  ") -> str:
    return "\n".join(pad + line for line in sql.splitlines())
