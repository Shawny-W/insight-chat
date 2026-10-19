"""归因引擎测试（不依赖 LLM，用真实 retail.db + 已初始化 meta.db）。

数据故事：2024-04 华东销量环比下滑 ~35%（~70% 来自产品线A），2024-05 回升 +25%。
归因 2024-04(A) vs 2024-05(B)：华东应为第一层 top 维值，下钻华东应出产品线A。
"""

import pytest

from chatbi.insight.attribution import AttributionError, attribute
from chatbi.insight.fluctuation import detect
from chatbi.semantic.store import load_snapshot


@pytest.fixture(scope="module")
def snap():
    return load_snapshot()


@pytest.fixture(scope="module")
def attr_result(snap):
    return attribute("metric_qty", "2024-04", "2024-05",
                     [{"dimension": "dim_channel", "op": "not_in", "values": ["TEST"]}],
                     snap, [])


def test_fluctuation_significant(snap):
    r = detect("metric_qty", "2024-04", "2024-05", snap, [])
    assert r["is_significant"] is True
    assert r["V_A"] < r["V_B"]                     # 4月低于5月
    assert r["pct"] is not None and r["pct"] < 0
    assert len(r["baseline_series"]) >= 6          # 基线充足（2023-06 起）
    assert r["baseline_limited"] is False


def test_fluctuation_normal_period(snap):
    r = detect("metric_qty", "2024-03", "2024-02", snap, [])
    assert r["is_significant"] is False            # 3月 vs 2月 为正常波动


def test_attribution_top_dim_and_value(attr_result):
    assert attr_result.is_significant is True
    assert attr_result.diffuse is False
    l1 = attr_result.drill_tree[0]
    assert l1["dimension_id"] == "dim_region"      # 第一层主因维度=大区
    assert l1["top"][0]["tech_value"] == "HD"      # 华东贡献最大
    assert l1["top"][0]["contrib"] < 0
    assert abs(l1["top"][0]["contrib_pct"]) >= 50  # 华东占比最高（噪声抵消后可超100%）


def test_attribution_drill_level2(attr_result):
    l2 = [n for n in attr_result.drill_tree if n["level"] == 2]
    assert l2, "应有第二层下钻"
    node = l2[0]
    assert node["path"] == ["华东"]                # 沿华东下钻
    assert node["dimension_id"] == "dim_product_line"
    assert node["top"][0]["tech_value"] == "A"     # 产品线A 为华东下滑主因
    assert node["top"][0]["contrib"] < 0


def test_attribution_events(attr_result):
    names = {e["name"] for e in attr_result.events}
    assert "华东物流线路检修" in names             # 与 periodA 时间重叠
    assert "五一促销主会场活动" in names           # 与 periodB 时间重叠


def test_attribution_waterfall(attr_result):
    wf = attr_result.waterfall
    assert wf[0]["type"] == "base" and wf[0]["value"] == attr_result.values["V_B"]
    assert wf[-1]["type"] == "total" and wf[-1]["value"] == attr_result.values["V_A"]
    assert any(x["type"] == "factor" for x in wf)


def test_attribution_to_dict_shape(attr_result):
    d = attr_result.to_dict()
    for key in ("metric", "period", "values", "baseline", "formula_split",
                "drill_tree", "events", "low_base_warnings", "waterfall",
                "excluded_dimensions_by_permission", "is_significant",
                "semantic_version"):
        assert key in d
    assert d["period"] == {"A": "2024-04", "B": "2024-05"}
    assert d["metric"]["name"] == "销量"


def test_derived_metric_formula_split(snap, monkeypatch):
    # 客单价 4月vs5月 实际波动不显著（早退路径属另一测试场景）；
    # 此处 monkeypatch detect 强制显著，验证 derivation 对数拆解逻辑本身（数据仍真实）。
    import chatbi.insight.attribution as att
    real_detect = att.detect

    def fake_detect(*a, **kw):
        r = real_detect(*a, **kw)
        r["is_significant"] = True
        return r

    monkeypatch.setattr(att, "detect", fake_detect)
    r = attribute("metric_aov", "2024-04", "2024-05", [], snap, [])
    assert r.formula_split is not None
    assert r.formula_split["numerator"]["metric_id"] == "metric_net_sales"
    assert r.formula_split["denominator"]["metric_id"] == "metric_order_cnt"
    assert 0 < r.formula_split["numerator"]["contrib_pct"] <= 100


def test_no_dimensions_raises(snap):
    import copy
    bad = copy.deepcopy(snap)
    bad.metrics["metric_qty"]["related_dimensions"] = []
    with pytest.raises(AttributionError) as ei:
        attribute("metric_qty", "2024-04", "2024-05", [], bad, [])
    assert ei.value.code == "ATTRIB_NO_DIMENSIONS"
