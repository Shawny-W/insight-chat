"""图表自动选择（PRD 6.4.2 规则 1/2/3/4/5/6/9/10/11，按优先级从上到下命中即停）。

choose(columns_meta, rows, semql) -> {type, reason, available_types}
columns_meta: [{"name":..., "role": "time"|"dimension"|"metric"|"column"}]
"""

from dataclasses import dataclass


@dataclass
class ChartChoice:
    type: str
    reason: str
    available_types: list[str]

    def to_dict(self) -> dict:
        return {"type": self.type, "reason": self.reason,
                "available_types": self.available_types}


def _roles(columns_meta: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    time_cols = [c for c in columns_meta if c.get("role") == "time"]
    dim_cols = [c for c in columns_meta if c.get("role") == "dimension"]
    metric_cols = [c for c in columns_meta if c.get("role") == "metric"]
    return time_cols, dim_cols, metric_cols


def _looks_percent(columns_meta: list[dict], semql) -> bool:
    """占比/份额语义启发式：指标列名含 占比/份额/率/%。"""
    for c in columns_meta:
        if c.get("role") == "metric" and any(
                w in c.get("name", "") for w in ("占比", "份额", "率", "%")):
            return True
    return False


def choose(columns_meta: list[dict], rows: list, semql=None,
           question: str | None = None) -> dict:
    """按 PRD 6.4.2 表序命中即停。返回 {type, reason, available_types}。"""
    n_rows = len(rows)
    time_cols, dim_cols, metric_cols = _roles(columns_meta)
    n_dim, n_metric, has_time = len(dim_cols), len(metric_cols), bool(time_cols)
    query_mode = (semql or {}).get("query_mode") if isinstance(semql, dict) else (
        getattr(semql, "query_mode", None) if semql is not None else None)

    def cat_names() -> list[str]:
        if not dim_cols:
            return []
        idx = columns_meta.index(dim_cols[0])
        return [str(r[idx]) for r in rows]

    # 规则11：归因结果（调用方传 waterfall 结构 rows：label/value/type）
    if columns_meta and all(c.get("name") in ("label", "value", "type")
                            for c in columns_meta):
        return ChartChoice("waterfall", "rule_11: 归因结果（贡献度分解）",
                           ["waterfall", "table"]).to_dict()

    # 规则9：明细
    if query_mode == "detail":
        return ChartChoice("table", "rule_9: query_mode=detail → 明细表格",
                           ["table"]).to_dict()

    # 规则1：单行单列指标卡
    if n_rows == 1 and n_metric == 1 and n_dim == 0 and not has_time:
        return ChartChoice("metric_card",
                         "rule_1: 单行单列（1个指标值，无维度）→ 指标卡",
                         ["metric_card", "table"]).to_dict()

    # 规则2：时间×1指标（行数≥3）→ 折线
    if has_time and n_metric == 1 and n_dim == 0 and n_rows >= 3:
        return ChartChoice("line",
                         f"rule_2: 时间维度×1指标, 行数{n_rows}≥3 → 折线图",
                         ["line", "bar", "table"]).to_dict()

    # 规则3：时间×1指标×1分类 → 多系列折线
    if has_time and n_metric == 1 and n_dim == 1:
        n_cat = len(set(cat_names()))
        if n_cat <= 8:
            return ChartChoice("multi_line",
                               f"rule_3: 时间×1指标×1分类, 分类数{n_cat}≤8 → 多系列折线图",
                               ["multi_line", "line", "table"]).to_dict()
        return ChartChoice("multi_line",
                           f"rule_3: 分类数{n_cat}>8 → 多系列折线（取top8其余合并其他）",
                           ["multi_line", "table"]).to_dict()

    # 规则4：分类×1指标 ≤12 → 柱状图（分类名平均长度>6 → 横向条形）
    if n_dim == 1 and n_metric == 1 and not has_time and 0 < n_rows <= 12:
        names = cat_names()
        avg_len = sum(len(n) for n in names) / max(len(names), 1)
        if avg_len > 6:
            return ChartChoice(
                "bar_horizontal",
                f"rule_4: 分类维度×1指标, 分类数{n_rows}≤12, 平均名称长度{avg_len:.0f}>6 → 横向条形图",
                ["bar", "bar_horizontal", "pie", "table"]).to_dict()
        return ChartChoice(
            "bar",
            f"rule_4: 分类维度×1指标, 分类数{n_rows}≤12 → 柱状图",
            ["bar", "bar_horizontal", "pie", "table"]).to_dict()

    # 规则5：分类×1指标 >12 → Top10 横向条形图
    if n_dim == 1 and n_metric == 1 and not has_time and n_rows > 12:
        return ChartChoice(
            "top10_bar_horizontal",
            f"rule_5: 分类维度×1指标, 分类数{n_rows}>12 → Top10横向条形图+其余合并其他（共{n_rows}类）",
            ["top10_bar_horizontal", "table"]).to_dict()

    # 规则6：分类×1指标 且占比语义 → 饼图（≤7）/矩形树图（表序在4/5之后，占比语义时前面规则未命中才会到这里）
    if n_dim == 1 and n_metric == 1 and _looks_percent(columns_meta, semql):
        if n_rows <= 7:
            return ChartChoice("pie", "rule_6: 占比语义, 分类数≤7 → 饼图",
                               ["pie", "bar", "table"]).to_dict()
        return ChartChoice("treemap", "rule_6: 占比语义, 分类数>7 → 矩形树图",
                           ["treemap", "table"]).to_dict()

    # 规则10：兜底透视表格
    if n_metric >= 1:
        return ChartChoice("pivot_table",
                           f"rule_10: 其他组合（指标{n_metric}×维度{n_dim}）不适用图形 → 透视表格",
                           ["pivot_table", "table"]).to_dict()

    return ChartChoice("table", "rule_10: 无法识别的数据形态 → 表格兜底",
                       ["table"]).to_dict()
