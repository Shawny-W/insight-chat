"""SemQL 模型与校验测试（不依赖 LLM）。依赖已初始化的 meta.db（scripts/init_db.py）。"""

import pytest

from chatbi.query.semql import (SemQL, resolve_time_range, today,
                                translate_dim_value)
from chatbi.semantic.store import load_snapshot


@pytest.fixture(scope="module")
def snap():
    return load_snapshot()


def _base() -> SemQL:
    return SemQL(
        dataset="ds_sales",
        metrics=["metric_net_sales"],
        dimensions=["dim_region"],
        time={"field": "orders.order_date", "range": "2024-04-01/2024-04-30",
              "grain": None},
        filters=[{"dimension": "dim_region", "op": "in", "values": ["HD"]}],
        limit=100,
        query_mode="aggregate",
    )


def test_valid_semql_passes(snap):
    assert _base().validate_against(snap) == []


def test_unknown_metric_rejected(snap):
    s = _base()
    s.metrics = ["metric_not_exist"]
    errors = s.validate_against(snap)
    assert any("SEMQL_INVALID_REF" in e and "metric_not_exist" in e for e in errors)


def test_metric_outside_dataset_rejected(snap):
    s = _base()
    s.metrics = ["metric_qty"]
    s.dataset = "ds_not_exist"
    errors = s.validate_against(snap)
    assert any("数据集" in e for e in errors)


def test_dim_value_translate(snap):
    dim = snap.dimensions["dim_region"]
    assert translate_dim_value(dim, "华东") == "HD"      # 展示名
    assert translate_dim_value(dim, "华东区") == "HD"    # 查询别名
    assert translate_dim_value(dim, "HD") == "HD"        # 技术值
    assert translate_dim_value(dim, "华南东") is None    # 无法翻译


def test_invalid_dim_value_rejected(snap):
    s = _base()
    s.filters[0].values = ["不存在区"]
    errors = s.validate_against(snap)
    assert any("SEMQL_INVALID_VALUE" in e for e in errors)


def test_limit_bounds(snap):
    s = _base()
    s.limit = 20000
    errors = s.validate_against(snap)
    assert any("SEMQL_INVALID_LIMIT" in e for e in errors)


def test_detail_limit_bounds(snap):
    s = _base()
    s.query_mode = "detail"
    s.metrics = []
    s.dimensions = ["col_orders_order_id"]
    s.limit = 5000
    errors = s.validate_against(snap)
    assert any("detail" in e and "SEMQL_INVALID_LIMIT" in e for e in errors)


def test_detail_requires_columns(snap):
    s = _base()
    s.query_mode = "detail"
    s.metrics = []
    s.dimensions = []
    errors = s.validate_against(snap)
    assert any("detail" in e for e in errors)


def test_business_term_validation(snap):
    s = _base()
    s.business_terms = ["term_old_customer"]
    assert s.validate_against(snap) == []
    s.business_terms = ["term_qty"]  # metric_term 不是 business_term
    assert any("business_term" in e for e in s.validate_against(snap))


def test_time_control_words():
    ref = today()
    start, end = resolve_time_range("last_month", ref)
    assert start < end and start.endswith("-01")
    with pytest.raises(ValueError):
        resolve_time_range("某个不知名的词", ref)
