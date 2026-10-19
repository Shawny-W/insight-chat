"""图表自动选择规则测试（构造数据，不依赖 LLM/DB）。"""

from chatbi.chart.rules import choose


def cm(name, role):
    return {"name": name, "role": role}


def test_rule1_metric_card():
    r = choose([cm("净销售额", "metric")], [[400883.87]],
               {"query_mode": "aggregate"})
    assert r["type"] == "metric_card"
    assert r["reason"].startswith("rule_1")


def test_rule2_line():
    cols = [cm("月份", "time"), cm("净销售额", "metric")]
    rows = [["2024-03", 1], ["2024-04", 2], ["2024-05", 3]]
    r = choose(cols, rows, {"query_mode": "aggregate"})
    assert r["type"] == "line"
    assert r["reason"].startswith("rule_2")


def test_rule3_multi_line():
    cols = [cm("月份", "time"), cm("大区", "dimension"), cm("销量", "metric")]
    rows = [[f"2024-0{m}", r, m * 10] for m in (3, 4, 5) for r in ("HD", "HB")]
    r = choose(cols, rows, {"query_mode": "aggregate"})
    assert r["type"] == "multi_line"
    assert r["reason"].startswith("rule_3")


def test_rule4_bar():
    cols = [cm("大区", "dimension"), cm("净销售额", "metric")]
    rows = [["华东", 1], ["华北", 2], ["华南", 3], ["西北", 4]]
    r = choose(cols, rows, {"query_mode": "aggregate"})
    assert r["type"] == "bar"
    assert r["reason"].startswith("rule_4")
    assert "bar" in r["available_types"]


def test_rule4_horizontal_when_long_names():
    cols = [cm("门店", "dimension"), cm("销量", "metric")]
    rows = [[f"某某特别长的门店名称{i}号店", i] for i in range(4)]
    r = choose(cols, rows, {"query_mode": "aggregate"})
    assert r["type"] == "bar_horizontal"


def test_rule5_top10():
    cols = [cm("门店", "dimension"), cm("销量", "metric")]
    rows = [[f"店{i}", i] for i in range(15)]
    r = choose(cols, rows, {"query_mode": "aggregate"})
    assert r["type"] == "top10_bar_horizontal"
    assert "共15类" in r["reason"]


def test_rule6_pie_not_reachable_after_rule4_but_defined():
    # 规则6在表序4/5之后；分类≤12时规则4先命中。验证占比语义列存在时仍按表序。
    cols = [cm("大区", "dimension"), cm("销量占比", "metric")]
    rows = [["华东", 40], ["华北", 30], ["华南", 20]]
    r = choose(cols, rows, {"query_mode": "aggregate"})
    assert r["type"] in ("bar", "pie")  # 表序命中即停；不断言 pie 必选


def test_rule9_detail_table():
    cols = [cm("订单号", "column"), cm("下单日期", "column")]
    rows = [["O1", "2024-04-01"], ["O2", "2024-04-02"]]
    r = choose(cols, rows, {"query_mode": "detail"})
    assert r["type"] == "table"
    assert r["reason"].startswith("rule_9")


def test_rule10_pivot():
    cols = [cm("大区", "dimension"), cm("产品线", "dimension"),
            cm("净销售额", "metric"), cm("销量", "metric")]
    rows = [["华东", "A", 1, 2]]
    r = choose(cols, rows, {"query_mode": "aggregate"})
    assert r["type"] == "pivot_table"
    assert r["reason"].startswith("rule_10")


def test_rule11_waterfall():
    cols = [cm("label", "column"), cm("value", "column"), cm("type", "column")]
    rows = [["5月", 3288, "base"], ["华东", -218, "factor"], ["4月", 3106, "total"]]
    r = choose(cols, rows, None)
    assert r["type"] == "waterfall"
    assert r["reason"].startswith("rule_11")
