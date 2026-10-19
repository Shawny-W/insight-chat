"""Schema Linking（契约见 spec 第 6 节）。

流程：jieba 词典路（优先锁定）→ 未命中 mention 走 LLM 精排（候选=同 dataset 全量元素）→
冲突检测（同 mention 多 Term 保留待澄清）。维度值只走词典精确命中，不走向量/LLM。
"""

import logging
import re
from dataclasses import dataclass, field

from chatbi.llm import chat_json
from chatbi.nlp import prompts
from chatbi.semantic.dictionary import segment
from chatbi.semantic.store import SemanticSnapshot

logger = logging.getLogger(__name__)


@dataclass
class LinkResult:
    matched_elements: list[dict] = field(default_factory=list)
    unmatched: list[dict] = field(default_factory=list)
    candidates_dropped: list[dict] = field(default_factory=list)


def _parse_nature(nature: str) -> dict | None:
    """把词典 nature 反解为 matched 元素。"""
    if nature.startswith("_dimval_"):
        body = nature[len("_dimval_"):]
        dim_id, _, tech = body.rpartition(":")
        return {"element_type": "dim_value", "element_id": dim_id, "tech_value": tech}
    if nature.startswith("_term_"):
        return {"element_type": "term", "element_id": nature[len("_term_"):]}
    if nature.startswith("_dim_"):
        return {"element_type": "dimension", "element_id": nature[len("_dim_"):]}
    if nature.startswith("_entity_"):
        return {"element_type": "entity", "element_id": nature[len("_entity_"):]}
    if nature.startswith("_attr_"):
        return {"element_type": "attribute", "element_id": nature[len("_attr_"):]}
    if nature.startswith("_dataset_"):
        return {"element_type": "dataset", "element_id": nature[len("_dataset_"):]}
    if nature.startswith("_model_"):
        return {"element_type": "model", "element_id": nature[len("_model_"):]}
    return None


def _dedup_add(result: LinkResult, element: dict) -> None:
    key = (element["element_type"], element["element_id"], element.get("tech_value"))
    for m in result.matched_elements:
        if (m["element_type"], m["element_id"], m.get("tech_value")) == key:
            return
    result.matched_elements.append(element)


def _dataset_terms(snapshot: SemanticSnapshot, dataset_id: str | None) -> list[dict]:
    """数据集范围内的术语（term_dataset_scope 映射过滤；无映射信息时返回全部）。"""
    scoped = [m["ont_id"] for m in snapshot.mappings
              if m["mapping_type"] == "term_dataset_scope" and m["mdl_id"] == dataset_id]
    if not scoped:
        return list(snapshot.terms.values())
    return [snapshot.terms[tid] for tid in scoped if tid in snapshot.terms]


def _candidate_lines(snapshot: SemanticSnapshot, dataset_id: str | None) -> tuple[list[str], dict[str, dict]]:
    """LLM 精排候选：同 dataset 的 term + dimension（名称 + 一句话定义摘要）。"""
    lines: list[str] = []
    index: dict[str, dict] = {}
    for t in _dataset_terms(snapshot, dataset_id):
        brief = (t.get("definition") or "")[:60]
        lines.append(f"- {t['id']} | {t['name']} | 术语 | {brief}")
        index[t["id"]] = {"element_type": "term", "element_id": t["id"]}
    ds = snapshot.datasets.get(dataset_id) if dataset_id else None
    dim_ids = ds.get("dimension_ids", []) if ds else list(snapshot.dimensions)
    for did in dim_ids:
        d = snapshot.dimensions.get(did)
        if not d:
            continue
        lines.append(f"- {d['id']} | {d['display_name']} | 维度 | 类型:{d.get('dim_type')}")
        index[d["id"]] = {"element_type": "dimension", "element_id": d["id"]}
    return lines, index


def _llm_rerank(question: str, mention: str, snapshot: SemanticSnapshot,
                dataset_id: str | None, result: LinkResult) -> None:
    """未命中 mention 的 LLM 精排；允许返回 NEED_CLARIFY。"""
    lines, index = _candidate_lines(snapshot, dataset_id)
    if not lines:
        result.unmatched.append({"mention": mention, "reason": "no_candidates"})
        return
    try:
        raw = chat_json("linking_rerank", prompts.RERANK_SYSTEM,
                        prompts.rerank_user(question, mention, lines),
                        heavy=False, temperature=0.0)
        chosen = raw.get("element_id")
        if chosen == "NEED_CLARIFY" or not chosen:
            result.unmatched.append({"mention": mention, "reason": "need_clarify"})
            return
        if chosen in index:
            _dedup_add(result, {"mention": mention, **index[chosen], "via": "llm"})
            for cid in index:
                if cid != chosen:
                    result.candidates_dropped.append(
                        {"mention": mention, "element_id": cid, "reason": "llm_not_selected"})
            return
        result.unmatched.append({"mention": mention, "reason": f"llm_hallucinated_id:{chosen}"})
    except Exception as exc:  # noqa: BLE001 - 精排失败降级：记 unmatched
        logger.warning("LLM 精排失败（mention=%s）: %s", mention, exc)
        result.unmatched.append({"mention": mention, "reason": f"rerank_failed:{exc}"})


def link(question: str, mentions: list[str], snapshot: SemanticSnapshot,
         dataset_id: str | None = None) -> LinkResult:
    """link(question, mentions, snapshot) -> LinkResult（契约签名固定）。"""
    result = LinkResult()
    matched_words: set[str] = set()

    # 路1：词典精确匹配（分词整词 + mention 原词）
    for word, nature in segment(question):
        if not nature.startswith("_"):
            continue
        parsed = _parse_nature(nature)
        if not parsed:
            continue
        matched_words.add(word)
        _dedup_add(result, {"mention": word, **parsed, "via": "dict"})

    # mention 级词典命中（LLM 意图阶段给的 mentions 可能是词典词的子串/原词）
    for mention in mentions or []:
        mention = (mention or "").strip()
        if not mention:
            continue
        hit = False
        for dim_id, tech, _disp in snapshot.resolve_dim_value(mention):
            _dedup_add(result, {"mention": mention, "element_type": "dim_value",
                                "element_id": dim_id, "tech_value": tech, "via": "dict"})
            hit = True
        for element_type, element_id in snapshot.lookup_alias(mention):
            spec_type = {"term": "term", "dimension": "dimension", "dataset": "dataset"}.get(
                element_type, element_type)
            _dedup_add(result, {"mention": mention, "element_type": spec_type,
                                "element_id": element_id, "via": "dict"})
            hit = True
        if hit:
            matched_words.add(mention)

    # 路3：未命中 mention 走 LLM 精排（维度值不走 LLM，只走词典）
    for mention in mentions or []:
        mention = (mention or "").strip()
        if not mention or mention in matched_words:
            continue
        if _is_time_mention(mention):
            continue  # 时间表达由意图阶段解析，不进语义匹配
        # 已被分词整词覆盖的 mention 跳过（如 "华东区" 已被切出）
        covered = any(m["mention"] in mention or mention in m["mention"]
                      for m in result.matched_elements)
        if covered:
            continue
        _llm_rerank(question, mention, snapshot, dataset_id, result)

    return result


_TIME_HINT = re.compile(r"(\d{4}\s*年|\d+\s*月|[日号]$|上个月|上月|本月|今年|去年|季度|周|最近|今天|昨天)")


def _is_time_mention(mention: str) -> bool:
    """粗判 mention 是否为时间表达（时间词不参与语义精排）。"""
    return bool(_TIME_HINT.search(mention))
