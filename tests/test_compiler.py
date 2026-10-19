"""SemQL 编译器测试（不依赖 LLM）。依赖已初始化的 meta.db。"""

import copy

import pytest

from chatbi.query.compiler import CompileError
from chatbi.query.compiler import compile as compile_semql
from chatbi.query.semql import SemQL
from chatbi.semantic.store import load_snapshot


@pytest.fixture(scope="module")
def snap():
    return load_snapshot()


def _semql(**over) -> SemQL:
    base = dict(
        dataset="ds_sales",
        metrics=["metric_net_sales"],
        dimensions=["dim_region"],
        time={"field": "orders.order_date", "range": "2024-04-01/2024-04-30",
              "grain": None},
        filters=[],
        order_by=[{"metric": "metric_net_sales", "direction": "desc"}],
        limit=100,
        query_mode="aggregate",
    )
    base.update(over)
    return SemQL(**base)


def test_basic_compile(snap):
    r = compile_semql(_semql(), snap)
    sql = r.sql
    assert "SUM(CASE WHEN" in sql
    assert "orders.status = 'completed'" in sql          # 度量口径 CASE WHEN
    assert "orders.channel != 'TEST'" in sql             # 测试渠道排除规则注入
    assert "BETWEEN '2024-04-01' AND '2024-04-30'" in sql
    assert "stores.region_name" in sql                   # 维度表达式
    assert "JOIN orders" in sql and "JOIN stores" in sql  # BFS join
    assert "ORDER BY" in sql and "LIMIT 100" in sql
    assert any("rule_exclude_test" in a for a in r.applied_rules)
    assert any("rule_completed" in a for a in r.applied_rules)
    assert r.trace["time"]["start"] == "2024-04-01"


def test_derived_metric_expansion(snap):
    r = compile_semql(_semql(metrics=["metric_aov"], dimensions=[]), snap)
    assert "NULLIF" in r.sql                              # 除零保护
    assert "COUNT(DISTINCT" in r.sql                      # 分母：有效订单数
    assert "quantity * order_items.unit_price" in r.sql.replace("\n", " ") or \
        "unit_price" in r.sql                             # 分子：净销售额


def test_mom_with_grain_uses_lag(snap):
    s = _semql(dimensions=[],
               time={"field": "orders.order_date",
                     "range": "2024-03-01/2024-05-31", "grain": "month"},
               compare={"type": "mom"})
    r = compile_semql(s, snap)
    assert "LAG(" in r.sql
    assert "strftime('%Y-%m'" in r.sql
    assert "环比" in r.sql


def test_mom_without_grain_two_periods(snap):
    s = _semql(dimensions=[], compare={"type": "mom"})
    r = compile_semql(s, snap)
    assert "净销售额_基期" in r.sql
    assert "2024-03-01" in r.sql                          # 基期平移一个月
    assert "差值" in r.sql


def test_dim_value_translation_in_filter(snap):
    s = _semql(filters=[{"dimension": "dim_region", "op": "in", "values": ["华东区"]}])
    r = compile_semql(s, snap)
    assert "stores.region_name IN ('HD')" in r.sql        # 展示别名→tech_value


def test_invalid_dim_value_raises(snap):
    s = _semql(filters=[{"dimension": "dim_region", "op": "in", "values": ["火星区"]}])
    with pytest.raises(CompileError) as ei:
        compile_semql(s, snap)
    assert ei.value.code == "SEMQL_INVALID_VALUE"


def test_fantrap_rejected(snap):
    bad = copy.deepcopy(snap)
    for rel in bad.relationships:
        if rel["id"] == "rel_items_orders":
            rel["join_type"] = "many_to_many"
    with pytest.raises(CompileError) as ei:
        compile_semql(_semql(), bad)
    assert ei.value.code == "COMPILE_FANTRAP_RISK"


def test_no_join_path(snap):
    bad = copy.deepcopy(snap)
    bad.models["mdl_island"] = {"id": "mdl_island", "name": "孤岛表",
                                "datasource_id": "ds_retail_local",
                                "source_type": "table", "table_ref": "island",
                                "primary_key": [], "time_field": None}
    bad.dimensions["dim_island"] = {"id": "dim_island", "model_id": "mdl_island",
                                    "name": "island_x", "display_name": "孤岛维度",
                                    "dim_type": "category", "expression": "island.x",
                                    "aliases": [], "value_maps": [],
                                    "hierarchy_path": None, "drill_enabled": 1}
    with pytest.raises(CompileError) as ei:
        compile_semql(_semql(dimensions=["dim_island"]), bad)
    assert ei.value.code == "COMPILE_NO_JOIN_PATH"


def test_business_term_inline(snap):
    s = _semql(business_terms=["term_old_customer"])
    r = compile_semql(s, snap)
    assert "customer_id IN" in r.sql                     # 老客户 rule_sql 内联
    assert "MIN(order_date)" in r.sql
    assert "date('2024-04-01','-1 year')" in r.sql        # {t_start} 代入
    assert any("老客户" in a for a in r.applied_rules)


def test_detail_mode(snap):
    s = _semql(metrics=[],
               dimensions=["col_orders_order_id", "col_orders_order_date",
                           "col_stores_store_name"],
               filters=[{"dimension": "dim_region", "op": "in", "values": ["HD"]}],
               limit=10, query_mode="detail")
    r = compile_semql(s, snap)
    assert "GROUP BY" not in r.sql
    assert "orders.order_id" in r.sql
    assert "LIMIT 10" in r.sql


def test_detail_limit_cap(snap):
    s = _semql(metrics=[], dimensions=["col_orders_order_id"],
               limit=500, query_mode="detail")
    r = compile_semql(s, snap)
    assert "LIMIT 500" in r.sql
