"""归因引擎（契约见 spec 第 6 节 + PRD 6.6.4；纯确定性算法，LLM 只做表述）。

attribute(metric_id, period_a, period_b, base_filters, snapshot, policies) -> AttributionResult
- contribution: contrib(d) = v_a(d) - v_b(d)；coverage(D) = Σ|contrib| top3 / |Δ_local|
- 选解释力最高维度 D*（coverage < 50% → 变化分散空态）
- 贪心下钻：|contrib|/|Δ_local| ≥ 20% 的节点继续，至多 3 层；hierarchy_path 优先
- z_score 校验低基数；business_event 时间重叠 + 维度值命中关联
"""

import math
import statistics
from dataclasses import asdict, dataclass, field

from chatbi.db import get_meta_conn
from chatbi.insight.fluctuation import detect, parse_period
from chatbi.query.compiler import compile as compile_semql
from chatbi.query.executor import execute
from chatbi.query.gateway import check_and_rewrite
from chatbi.query.semql import SemQL
from chatbi.semantic.store import SemanticSnapshot

MAX_DRILL_LEVELS = 3
DRILL_DOWN_THRESHOLD = 0.20   # top 维值贡献占比 ≥20% 继续下钻
COVERAGE_THRESHOLD = 0.50     # 维度解释力阈值
STOP_CONTRIB_RATIO = 0.10     # 节点 |contrib|/|Δ_global| < 10% 停止


class AttributionError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


@dataclass
class AttributionResult:
    metric: dict
    period: dict
    values: dict
    baseline: dict
    formula_split: dict | None
    drill_tree: list[dict]
    events: list[dict]
    low_base_warnings: list[str]
    waterfall: list[dict]
    excluded_dimensions_by_permission: list[str]
    is_significant: bool
    diffuse: bool
    semantic_version: int

    def to_dict(self) -> dict:
        return asdict(self)


# ---------- 两期分组查询（复用查询链路） ----------

def _two_period_by_dim(snapshot: SemanticSnapshot, policies: list[dict] | None,
                       metric_id: str, dim_id: str,
                       pa: tuple[str, str], pb: tuple[str, str],
                       filters: list[dict]) -> dict[str, tuple[float, float]]:
    """对维度 D 一条 SQL 拿两期各维值指标：返回 {tech_value: (v_a, v_b)}。"""
    semql = SemQL(dataset=next(iter(snapshot.datasets)), metrics=[metric_id],
                  dimensions=[dim_id], filters=filters,
                  time={"field": None, "range": f"{pa[0]}/{pa[1]}", "grain": None},
                  compare={"type": "period_vs_period", "period_b": f"{pb[0]}/{pb[1]}"},
                  limit=10000, query_mode="aggregate")
    compiled = compile_semql(semql, snapshot)
    sql = check_and_rewrite(compiled.sql, policies or [])
    rs = execute(sql)
    # 列序：维度值, 现期, 基期, 差值, 对比%
    return {str(r[0]): (float(r[1] or 0), float(r[2] or 0)) for r in rs.rows}


def _monthly_by_dim(snapshot: SemanticSnapshot, policies: list[dict] | None,
                    metric_id: str, dim_id: str, start: str, end: str,
                    filters: list[dict]) -> dict[str, list[float]]:
    """维度值×月 的指标序列（z_score 用）：{tech_value: [v_月度...]}。"""
    semql = SemQL(dataset=next(iter(snapshot.datasets)), metrics=[metric_id],
                  dimensions=[dim_id], filters=filters,
                  time={"field": None, "range": f"{start}/{end}", "grain": "month"},
                  limit=10000, query_mode="aggregate")
    compiled = compile_semql(semql, snapshot)
    sql = check_and_rewrite(compiled.sql, policies or [])
    rs = execute(sql)
    out: dict[str, list[float]] = {}
    # 列序：月份, 维度值, 指标
    for month, dimval, v in rs.rows:
        out.setdefault(str(dimval), []).append(float(v or 0))
    return out


# ---------- 维度池与权限过滤 ----------

def _dim_pool(snapshot: SemanticSnapshot, metric: dict,
              policies: list[dict] | None) -> tuple[list[str], list[str]]:
    """候选维度池 = metric.related_dimensions，剔除被列策略 hide 的维度。"""
    pool = list(metric.get("related_dimensions") or [])
    hidden_dims: set[str] = set()
    for p in policies or []:
        if p.get("policy_type") == "column" and p.get("column_action") == "hide":
            col = p.get("column_name")
            for did, d in snapshot.dimensions.items():
                expr = d.get("expression") or ""
                if col and expr.endswith(f".{col}"):
                    hidden_dims.add(did)
    kept = [d for d in pool if d not in hidden_dims]
    excluded = [snapshot.dimensions[d]["display_name"] for d in pool if d in hidden_dims]
    return kept, excluded


def _hierarchy_rank(snapshot: SemanticSnapshot, dim_id: str) -> int:
    """hierarchy_path 优先：在 dim_region 层级路径中的位置；不在则排后。"""
    path = snapshot.dimensions.get("dim_region", {}).get("hierarchy_path") or []
    name = snapshot.dimensions.get(dim_id, {}).get("display_name")
    if name in path:
        return path.index(name)
    return len(path) + 1


def _dim_unlocked(snapshot: SemanticSnapshot, dim_id: str,
                  used_dims: set[str]) -> bool:
    """hierarchy 链锁层：维度在 hierarchy_path 中时，其全部上级维度必须已下钻过才解锁。
    链外维度（产品线/渠道等）始终可参选。"""
    for d in snapshot.dimensions.values():
        path = d.get("hierarchy_path") or []
        name = snapshot.dimensions.get(dim_id, {}).get("display_name")
        if name not in path:
            continue
        idx = path.index(name)
        if idx == 0:
            return True
        # 任一上级未使用 → 锁定
        for parent_name in path[:idx]:
            parent_id = next((k for k, v in snapshot.dimensions.items()
                              if v.get("display_name") == parent_name), None)
            if parent_id and parent_id not in used_dims:
                return False
    return True


# ---------- 主入口 ----------

def attribute(metric_id: str, period_a, period_b, base_filters: list[dict] | None,
              snapshot: SemanticSnapshot,
              policies: list[dict] | None = None) -> AttributionResult:
    """归因主入口（契约签名固定）。"""
    base_filters = list(base_filters or [])
    metric = snapshot.metrics.get(metric_id)
    if metric is None:
        raise AttributionError("ATTRIB_NO_METRIC", f"指标 {metric_id} 不存在")
    term = snapshot.term_of_metric(metric_id) or {}

    pa, pb = parse_period(period_a), parse_period(period_b)
    fluc = detect(metric_id, pa, pb, snapshot, policies, base_filters)
    v_a, v_b, delta = fluc["V_A"], fluc["V_B"], fluc["delta"]
    period_label = {"A": pa[0][:7], "B": pb[0][:7]}

    baseline = {"mom_mean": fluc["mu"], "mom_std": fluc["sigma"],
                "is_significant": fluc["is_significant"],
                "series": fluc["baseline_series"],
                "baseline_limited": fluc["baseline_limited"]}
    values = {"V_A": v_a, "V_B": v_b, "delta": delta, "pct": fluc["pct"]}
    metric_info = {"id": metric_id, "name": term.get("name", metric_id),
                   "direction": term.get("direction"), "unit": term.get("unit")}

    if not fluc["is_significant"]:
        return AttributionResult(metric=metric_info, period=period_label, values=values,
                                 baseline=baseline, formula_split=None, drill_tree=[],
                                 events=[], low_base_warnings=[], waterfall=[],
                                 excluded_dimensions_by_permission=[],
                                 is_significant=False, diffuse=False,
                                 semantic_version=snapshot.version)

    # ---- derivation 公式拆解（derived 指标先拆分子/分母） ----
    formula_split = None
    drill_metric_id = metric_id
    if metric.get("metric_kind") == "derived":
        parts = [p.strip() for p in (metric.get("formula") or "").split("/")]
        if len(parts) == 2 and all(p in snapshot.metrics for p in parts):
            num_id, den_id = parts
            x_a = _scalar(snapshot, policies, num_id, pa, base_filters)
            x_b = _scalar(snapshot, policies, num_id, pb, base_filters)
            y_a = _scalar(snapshot, policies, den_id, pa, base_filters)
            y_b = _scalar(snapshot, policies, den_id, pb, base_filters)
            if all(v > 0 for v in (x_a, x_b, y_a, y_b)) and v_a > 0 and v_b > 0:
                # 对数分解：ln(M_A/M_B) = ln(X_A/X_B) - ln(Y_A/Y_B)
                ln_m = math.log(v_a / v_b)
                c_num = math.log(x_a / x_b)
                c_den = -math.log(y_a / y_b)
                total = abs(c_num) + abs(c_den)
                if abs(ln_m) > 1e-9 and total > 1e-9:
                    num_term = snapshot.term_of_metric(num_id) or {}
                    den_term = snapshot.term_of_metric(den_id) or {}
                    formula_split = {
                        "formula": metric.get("formula"),
                        "numerator": {"metric_id": num_id,
                                      "name": num_term.get("name", num_id),
                                      "v_a": x_a, "v_b": x_b,
                                      "contrib_pct": round(abs(c_num) / total * 100, 1)},
                        "denominator": {"metric_id": den_id,
                                        "name": den_term.get("name", den_id),
                                        "v_a": y_a, "v_b": y_b,
                                        "contrib_pct": round(abs(c_den) / total * 100, 1)},
                    }
                    drill_metric_id = (num_id if abs(c_num) >= abs(c_den) else den_id)

    drill_metric = snapshot.metrics[drill_metric_id]
    pool, excluded = _dim_pool(snapshot, drill_metric, policies)
    if not pool:
        raise AttributionError("ATTRIB_NO_DIMENSIONS",
                               f"指标 {drill_metric_id} 暂未配置可分析维度，已通知数据管理员")

    # ---- 贪心下钻 ----
    drill_tree: list[dict] = []
    low_base_warnings: list[str] = []
    z_series_cache: dict[tuple, dict[str, list[float]]] = {}
    hist_start = "2023-06-01"  # 演示数据起点

    def z_score(dim_id: str, tech: str, fixed: list[dict], v_a_d: float) -> float | None:
        key = (dim_id, tuple(sorted(f["values"][0] for f in fixed)))
        if key not in z_series_cache:
            z_series_cache[key] = _monthly_by_dim(
                snapshot, policies, drill_metric_id, dim_id, hist_start, pb[1],
                base_filters + fixed)
        series = z_series_cache[key].get(tech, [])
        if len(series) < 3:
            return None
        mu = statistics.mean(series)
        sd = statistics.pstdev(series)
        return (v_a_d - mu) / sd if sd > 0 else None

    # (level, fixed_filters, path_display, delta_local)
    frontier: list[tuple[int, list[dict], list[str], float]] = [
        (1, [], [], delta)]
    while frontier:
        level, fixed, path, delta_local = frontier.pop(0)
        if level > MAX_DRILL_LEVELS or abs(delta_local) < 1e-9:
            continue
        used_dims = {f["dimension"] for f in fixed}
        candidates = [d for d in pool if d not in used_dims
                      and _dim_unlocked(snapshot, d, used_dims)]

        best = None  # (dim_id, contribs, coverage, top3)；决胜：coverage → top1|contrib|
        best_key: tuple[float, float] | None = None
        for dim_id in candidates:
            data = _two_period_by_dim(snapshot, policies, drill_metric_id, dim_id,
                                      pa, pb, base_filters + fixed)
            if not data:
                continue
            contribs = sorted(
                ((tech, va - vb, va, vb) for tech, (va, vb) in data.items()),
                key=lambda t: abs(t[1]), reverse=True)
            top3 = contribs[:3]
            coverage = sum(abs(c) for _, c, _, _ in top3) / abs(delta_local)
            key = (round(coverage, 6), abs(top3[0][1]))
            if best is None or key > best_key:
                best = (dim_id, contribs, coverage, top3)
                best_key = key
        if best is None or best[2] < COVERAGE_THRESHOLD:
            if level == 1:
                # 变化分散空态
                return AttributionResult(
                    metric=metric_info, period=period_label, values=values,
                    baseline=baseline, formula_split=formula_split, drill_tree=[],
                    events=[], low_base_warnings=[], waterfall=[],
                    excluded_dimensions_by_permission=excluded,
                    is_significant=True, diffuse=True,
                    semantic_version=snapshot.version)
            continue

        dim_id, contribs, coverage, top3 = best
        dim = snapshot.dimensions[dim_id]
        node_top = []
        for tech, contrib, va_d, vb_d in top3:
            disp = next((vm["display_value"] for vm in dim.get("value_maps", [])
                         if str(vm["tech_value"]) == tech), tech)
            z = z_score(dim_id, tech, fixed, va_d)
            low_base = (z is not None and abs(z) < 1
                        and abs(v_b) > 0 and abs(vb_d / v_b) < 0.01)
            if low_base:
                low_base_warnings.append(
                    f"{dim['display_name']}={disp} 为低基数波动（|z|={abs(z):.2f}<1），参考意义有限")
            node_top.append({
                "value": disp, "tech_value": tech,
                "contrib": round(contrib, 2),
                "contrib_pct": round(contrib / abs(delta_local) * 100, 1),
                "v_a": va_d, "v_b": vb_d,
                "z_score": round(z, 2) if z is not None else None,
                "low_base": low_base,
            })
        drill_tree.append({
            "level": level, "path": path, "dimension": dim["display_name"],
            "dimension_id": dim_id, "top": node_top,
            "coverage": round(coverage, 3),
        })

        # 继续下钻：|contrib|/|Δ_local| ≥ 20% 的节点
        for tech, contrib, _va, _vb in top3:
            if abs(contrib) / abs(delta) < STOP_CONTRIB_RATIO:
                continue
            if abs(contrib) / abs(delta_local) < DRILL_DOWN_THRESHOLD:
                continue
            disp = next((vm["display_value"] for vm in dim.get("value_maps", [])
                         if str(vm["tech_value"]) == tech), tech)
            frontier.append((level + 1,
                             fixed + [{"dimension": dim_id, "op": "in", "values": [tech]}],
                             path + [disp], contrib))

    # ---- waterfall（V_B → 第1层 top 因子 → 其他 → V_A） ----
    waterfall: list[dict] = [{"label": f"{period_label['B']}", "value": v_b, "type": "base"}]
    if drill_tree:
        l1 = drill_tree[0]
        for t in l1["top"]:
            waterfall.append({"label": f"{l1['dimension']}:{t['value']}",
                              "value": t["contrib"], "type": "factor"})
        other = delta - sum(t["contrib"] for t in l1["top"])
        if abs(other) > 1e-9:
            waterfall.append({"label": "其他", "value": round(other, 2), "type": "factor"})
    waterfall.append({"label": f"{period_label['A']}", "value": v_a, "type": "total"})

    # ---- 事件关联（时间重叠 + 归因路径维度值命中） ----
    events = _match_events(pa, pb, drill_tree)

    return AttributionResult(
        metric=metric_info, period=period_label, values=values, baseline=baseline,
        formula_split=formula_split, drill_tree=drill_tree, events=events,
        low_base_warnings=low_base_warnings, waterfall=waterfall,
        excluded_dimensions_by_permission=excluded,
        is_significant=True, diffuse=False, semantic_version=snapshot.version)


def _scalar(snapshot: SemanticSnapshot, policies: list[dict] | None, metric_id: str,
            period: tuple[str, str], filters: list[dict]) -> float:
    from chatbi.insight.fluctuation import _run_scalar  # 复用单值查询
    return _run_scalar(snapshot, policies, metric_id, period[0], period[1], filters)


def _match_events(pa: tuple[str, str], pb: tuple[str, str],
                  drill_tree: list[dict]) -> list[dict]:
    """business_event 关联：区间与 A 期或 A~B 相交 且（全局事件 或 命中归因路径维度值）。"""
    path_values = {(n["dimension_id"], t["tech_value"])
                   for n in drill_tree for t in n["top"]}
    lo, hi = pa[0], pb[1]
    conn = get_meta_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM business_event WHERE start_date <= ? AND end_date >= ?",
            (hi, lo),
        ).fetchall()
    finally:
        conn.close()
    events = []
    for r in rows:
        r = dict(r)
        if r["related_dimension"] is not None and \
                (r["related_dimension"], r["related_value"]) not in path_values:
            continue
        overlap = []
        if r["start_date"] <= pa[1] and r["end_date"] >= pa[0]:
            overlap.append("periodA")
        if r["start_date"] <= pb[1] and r["end_date"] >= pb[0]:
            overlap.append("periodB")
        events.append({
            "name": r["event_name"], "type": r["event_type"],
            "start_date": r["start_date"], "end_date": r["end_date"],
            "related": {"dimension": r["related_dimension"], "value": r["related_value"]},
            "note": r["impact_note"],
            "overlap": "both" if len(overlap) == 2 else (overlap[0] if overlap else "range"),
        })
    return events
