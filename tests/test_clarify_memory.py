"""会话内澄清选择记忆测试（不依赖 LLM）。依赖已初始化的 meta.db。

覆盖：同一会话内用户澄清“销售额”→净销售额后，追问再次出现同一 TERM_CONFLICT
时应自动沿用而不再发澄清卡；记忆失效（候选变化）时照常追问。
"""

from types import SimpleNamespace

import pytest

from chatbi.nlp.linking import LinkResult
from chatbi.semantic.store import load_snapshot
from chatbi.services.chat_service import _apply_clarify_memory, _merge_choice


@pytest.fixture(scope="module")
def snap():
    return load_snapshot()


def _link_with_sales_conflict() -> LinkResult:
    """模拟“销售额”同时命中净/毛销售额两个 term 的 link 结果。"""
    return LinkResult(matched_elements=[
        {"element_type": "term", "element_id": "term_net_sales", "mention": "销售额"},
        {"element_type": "term", "element_id": "term_gross_sales", "mention": "销售额"},
        {"element_type": "dim_value", "element_id": "dim_region",
         "mention": "华东区", "tech_value": "HD"},
    ])


def _term_conflict_amb() -> dict:
    return {"type": "TERM_CONFLICT", "mention": "销售额",
            "candidates": ["term_net_sales", "term_gross_sales"],
            "reason": "“销售额”同时是多个指标的别名"}


def test_merge_choice_term_conflict():
    link_result = _link_with_sales_conflict()
    ok = _merge_choice("TERM_CONFLICT", "销售额", "term_net_sales",
                       SimpleNamespace(), link_result)
    assert ok
    term_ids = [m["element_id"] for m in link_result.matched_elements
                if m["element_type"] == "term"]
    assert term_ids == ["term_net_sales"]
    # 维度值命中不受影响
    assert any(m["element_type"] == "dim_value" for m in link_result.matched_elements)


def test_merge_choice_term_fallback_append():
    """所选 term 不在命中列表时兜底补一条命中。"""
    link_result = LinkResult(matched_elements=[
        {"element_type": "term", "element_id": "term_gross_sales", "mention": "销售额"},
    ])
    ok = _merge_choice("TERM_CONFLICT", "销售额", "term_net_sales",
                       SimpleNamespace(), link_result)
    assert ok
    term_ids = [m["element_id"] for m in link_result.matched_elements
                if m["element_type"] == "term"]
    assert "term_net_sales" in term_ids


def test_apply_memory_reuses_choice(snap):
    ctx = {"clarify_choices": {"term:销售额": "term_net_sales"}}
    link_result = _link_with_sales_conflict()
    remaining, notes = _apply_clarify_memory(
        [_term_conflict_amb()], SimpleNamespace(), link_result, ctx, snap)
    assert remaining == []
    assert len(notes) == 1 and "净销售额" in notes[0]
    term_ids = [m["element_id"] for m in link_result.matched_elements
                if m["element_type"] == "term"]
    assert term_ids == ["term_net_sales"]


def test_apply_memory_stale_choice_ignored(snap):
    """记忆中的 term 已不在候选内（语义层变更）→ 照常追问。"""
    ctx = {"clarify_choices": {"term:销售额": "term_deleted_sales"}}
    link_result = _link_with_sales_conflict()
    amb = _term_conflict_amb()
    remaining, notes = _apply_clarify_memory(
        [amb], SimpleNamespace(), link_result, ctx, snap)
    assert remaining == [amb]
    assert notes == []


def test_apply_memory_value_conflict(snap):
    ctx = {"clarify_choices": {"value:华东": "dim_region|HD"}}
    link_result = LinkResult(matched_elements=[
        {"element_type": "dim_value", "element_id": "dim_region",
         "mention": "华东", "tech_value": "HD"},
        {"element_type": "dim_value", "element_id": "dim_city",
         "mention": "华东", "tech_value": "HD-city"},
    ])
    amb = {"type": "VALUE_CONFLICT", "mention": "华东",
           "candidates": ["dim_region:HD(华东区)", "dim_city:HD-city(华东市)"],
           "reason": "“华东”可映射到多个维度取值"}
    remaining, notes = _apply_clarify_memory(
        [amb], SimpleNamespace(), link_result, ctx, snap)
    assert remaining == []
    assert len(notes) == 1 and "华东区" in notes[0]
    dim_hits = [m for m in link_result.matched_elements
                if m["element_type"] == "dim_value" and m["mention"] == "华东"]
    assert len(dim_hits) == 1 and dim_hits[0]["element_id"] == "dim_region"


def test_apply_memory_no_memory_passthrough(snap):
    ctx = {"clarify_choices": {}}
    amb = _term_conflict_amb()
    remaining, notes = _apply_clarify_memory(
        [amb], SimpleNamespace(), _link_with_sales_conflict(), ctx, snap)
    assert remaining == [amb]
    assert notes == []
