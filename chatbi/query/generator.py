"""SemQL 生成器（LLM，重模型）+ PRD 6.3.6 三级重试链。

generate(question, link_result, snapshot, exemplars, ctx, errors=[]) -> SemQL
重试链：原 prompt(temperature=0) → 错误回显(temperature=0) → 追加 L3 候选扩展(temperature=0.3)。
仍失败抛 LLMGenerateFailedError（携带全部错误记录）。
"""

import logging

from pydantic import ValidationError

from chatbi.llm import LLMFormatError, chat_json
from chatbi.nlp import prompts
from chatbi.nlp.intent import today
from chatbi.nlp.linking import LinkResult
from chatbi.query.semql import SemQL
from chatbi.semantic.store import SemanticSnapshot

logger = logging.getLogger(__name__)


class LLMGenerateFailedError(Exception):
    """三级重试均失败。"""

    def __init__(self, errors: list[str]):
        super().__init__("LLM 三次生成 SemQL 均失败: " + " | ".join(errors))
        self.errors = errors


# ---------- prompt 内容构建（L1/L2/L3） ----------

def _metric_manifest(snapshot: SemanticSnapshot, ds: dict) -> list[str]:
    lines = []
    for mid in ds.get("metric_ids", []):
        m = snapshot.metrics.get(mid)
        if not m:
            continue
        term = snapshot.term_of_metric(mid) or {}
        brief = (term.get("definition") or "")[:40]
        unit = (m.get("data_format") or {}).get("unit", "")
        agg = m.get("agg") or "derived"
        lines.append(f"- {mid} | {term.get('name', mid)} | {brief} | 单位:{unit} | 默认聚合:{agg}")
    return lines


def _dimension_manifest(snapshot: SemanticSnapshot, ds: dict) -> list[str]:
    lines = []
    for did in ds.get("dimension_ids", []):
        d = snapshot.dimensions.get(did)
        if not d:
            continue
        path = "/".join(d.get("hierarchy_path") or [])
        lines.append(f"- {did} | {d['display_name']} | 类型:{d.get('dim_type')} | 下钻路径:{path}")
    # 明细列（detail 模式可用，col_xxx）
    model_ids = set(ds.get("model_ids", []))
    for c in snapshot.columns:
        if c["model_id"] in model_ids:
            table = snapshot.models[c["model_id"]]["table_ref"]
            lines.append(f"- {c['id']} | {c['display_name']} | 明细列:{table}.{c['name']}")
    return lines


def _matched_detail_lines(snapshot: SemanticSnapshot,
                          link_result: LinkResult) -> tuple[list[str], str | None]:
    """L2 命中元素详情；返回 (详情行, 术语冲突提示)。"""
    lines: list[str] = []
    term_conflict_hint = None
    seen_terms: set[str] = set()

    for m in link_result.matched_elements:
        etype, eid = m["element_type"], m["element_id"]
        if etype == "term" and eid in snapshot.terms and eid not in seen_terms:
            seen_terms.add(eid)
            t = snapshot.terms[eid]
            metric = snapshot.metric_of_term(eid)
            block = [f"## 术语 {t['name']} ({eid})",
                     f"口径定义: {t.get('definition', '')}",
                     f"单位: {t.get('unit') or '无'}；方向: {t.get('direction') or 'neutral'}"]
            if t.get("term_type") == "business_term":
                block.append("该词为业务术语（business_term），生成时把其 id 填入 business_terms 数组")
            if metric:
                block.append(f"绑定指标: {metric['id']}（{'原子' if metric['metric_kind'] == 'atomic' else '派生'}）")
            rules = [r for r in snapshot.rules_of_term(eid) if r.get("severity") != "info"]
            if rules:
                block.append("必须遵守的业务规则:")
                for r in rules:
                    block.append(f"- [{r['severity']}] {r['description']}")
            for rel in snapshot.relations:
                if rel.get("relation_type") == "derivation" and rel.get("from_id") == eid:
                    block.append(f"推导关系: {rel.get('formula')}（{rel.get('note', '')}）")
            lines.append("\n".join(block))
        elif etype == "dimension" and eid in snapshot.dimensions:
            d = snapshot.dimensions[eid]
            block = [f"## 维度 {d['display_name']} ({eid})"]
            if d.get("value_maps"):
                maps = "，".join(f"{vm['display_value']}→{vm['tech_value']}"
                                for vm in d["value_maps"])
                block.append(f"值映射(展示名→tech_value): {maps}")
                aliases = [(qa, vm["tech_value"]) for vm in d["value_maps"]
                           for qa in vm.get("query_aliases", [])]
                if aliases:
                    block.append("查询别名: " + "，".join(f"{a}→{t}" for a, t in aliases))
            lines.append("\n".join(block))
        elif etype == "dim_value" and eid in snapshot.dimensions:
            d = snapshot.dimensions[eid]
            disp = next((vm["display_value"] for vm in d.get("value_maps", [])
                         if vm["tech_value"] == m.get("tech_value")), m.get("tech_value"))
            lines.append(f"## 维度值命中: “{m['mention']}” → 维度 {d['display_name']}({eid})，"
                         f"tech_value={m.get('tech_value')}（展示名:{disp}），"
                         f"过滤条件写 filters: [{{\"dimension\": \"{eid}\", \"op\": \"in\", "
                         f"\"values\": [\"{m.get('tech_value')}\"]}}]")

    # 共享别名冲突提示（如"销售额"）
    mention_terms: dict[str, set[str]] = {}
    for m in link_result.matched_elements:
        if m["element_type"] == "term":
            mention_terms.setdefault(m["mention"], set()).add(m["element_id"])
    for mention, tids in mention_terms.items():
        if len(tids) >= 2:
            names = "、".join(snapshot.terms[t]["name"] for t in sorted(tids) if t in snapshot.terms)
            term_conflict_hint = f"“{mention}”同时是以下指标的别名：{names}（口径歧义已在澄清环节解决，按用户确认口径输出）"
    return lines, term_conflict_hint


def _candidate_lines(snapshot: SemanticSnapshot, ds: dict,
                     link_result: LinkResult) -> list[str]:
    """L3 候选扩展：dataset 内未命中元素（仅名称+类型）。"""
    hit_ids = {m["element_id"] for m in link_result.matched_elements}
    lines = []
    for mid in ds.get("metric_ids", []):
        if mid in hit_ids or mid not in snapshot.metrics:
            continue
        term = snapshot.term_of_metric(mid) or {}
        lines.append(f"- {mid} | {term.get('name', mid)} | 指标")
    for did in ds.get("dimension_ids", []):
        if did in hit_ids or did not in snapshot.dimensions:
            continue
        lines.append(f"- {did} | {snapshot.dimensions[did]['display_name']} | 维度")
    return lines


# ---------- 主入口 ----------

def generate(question: str, link_result: LinkResult, snapshot: SemanticSnapshot,
             exemplars: list[dict] | None = None, ctx: list[dict] | None = None,
             errors: list[str] | None = None) -> SemQL:
    """generate(...) -> SemQL（契约签名固定；含三级重试）。"""
    dataset_id = next((m["element_id"] for m in link_result.matched_elements
                       if m["element_type"] == "dataset"), None)
    ds = snapshot.datasets.get(dataset_id) or next(iter(snapshot.datasets.values()))
    dataset_id = ds["id"]

    metric_manifest = _metric_manifest(snapshot, ds)
    dimension_manifest = _dimension_manifest(snapshot, ds)
    matched_details, term_conflict_hint = _matched_detail_lines(snapshot, link_result)
    exemplar_lines = [prompts.exemplar_line(e) for e in (exemplars or [])[:3]]
    ctx_lines = [f"前一轮: 问题={t.get('question', '')}" for t in (ctx or [])[-5:]]
    current_date = today().isoformat()

    base_user = prompts.semql_user(
        question, current_date, ds, metric_manifest, dimension_manifest,
        matched_details, [], exemplar_lines, ctx_lines,
        term_conflict_hint=term_conflict_hint)

    all_errors: list[str] = list(errors or [])
    # 三级重试链：(prompt变体, temperature)
    attempts = [(base_user, 0.0), (None, 0.0), (None, 0.3)]  # 1/2 级在循环内构建
    for attempt in range(3):
        if attempt == 1:
            user = prompts.semql_retry_user(base_user, all_errors)
        elif attempt == 2:
            user = prompts.semql_user(
                question, current_date, ds, metric_manifest, dimension_manifest,
                matched_details, _candidate_lines(snapshot, ds, link_result),
                exemplar_lines, ctx_lines, term_conflict_hint=term_conflict_hint)
            user = prompts.semql_retry_user(user, all_errors)
        else:
            user = attempts[0][0]
        temperature = attempts[attempt][1]

        try:
            raw = chat_json("semql_generate", prompts.SEMQL_SYSTEM, user,
                            heavy=True, temperature=temperature)
            raw["dataset"] = dataset_id
            semql = SemQL.model_validate(raw)
            errs = semql.validate_against(snapshot)
            if errs:
                all_errors.extend(errs)
                logger.warning("SemQL 校验失败（第 %d 次）: %s", attempt + 1, errs)
                continue
            return semql
        except (LLMFormatError, ValidationError, TypeError) as exc:
            all_errors.append(f"第{attempt + 1}次生成解析失败: {exc}")
            logger.warning("SemQL 解析失败（第 %d 次）: %s", attempt + 1, exc)

    raise LLMGenerateFailedError(all_errors)
