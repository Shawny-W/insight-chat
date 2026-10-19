"""澄清管理（契约见 spec 第 6 节：build_clarify(ambiguities, snapshot, round) -> ClarifyCard | None）。

歧义检测为规则复核（PRD 6.1.4 五条，确定性非 LLM）；卡片文案用 PRD 6.1.3 上线文案。
"""

import uuid
from dataclasses import asdict, dataclass, field

from chatbi.db import get_meta_conn
from chatbi.nlp import prompts
from chatbi.nlp.intent import IntentResult
from chatbi.nlp.linking import LinkResult
from chatbi.semantic.store import SemanticSnapshot

MAX_CLARIFY_ROUNDS = 2


@dataclass
class ClarifyCard:
    clarify_id: str
    type: str
    prompt: str
    options: list[dict] = field(default_factory=list)  # [{"key":..., "label":...}]
    allow_free_text: bool = True
    round: int = 1
    max_rounds: int = MAX_CLARIFY_ROUNDS

    def to_dict(self) -> dict:
        return asdict(self)


# ---------- 歧义检测（规则复核，确定性） ----------

def detect_ambiguities(question: str, intent: IntentResult, link_result: LinkResult,
                       snapshot: SemanticSnapshot) -> list[dict]:
    """按 PRD 6.1.4 五条规则复核，返回有效歧义列表。"""
    ambiguities: list[dict] = []

    # 1. TERM_CONFLICT：同一 mention 命中 ≥2 个绑定不同 Metric 的 published Term
    term_hits: dict[str, list[str]] = {}
    for m in link_result.matched_elements:
        if m["element_type"] == "term":
            term_hits.setdefault(m["mention"], [])
            if m["element_id"] not in term_hits[m["mention"]]:
                term_hits[m["mention"]].append(m["element_id"])
    for mention, term_ids in term_hits.items():
        metric_terms = [t for t in term_ids
                        if snapshot.terms.get(t, {}).get("term_type") == "metric_term"]
        if len(metric_terms) < 2:
            continue
        bound_metrics = {snapshot.metric_of_term(t)["id"] for t in metric_terms
                         if snapshot.metric_of_term(t)}
        if len(bound_metrics) >= 2:
            ambiguities.append({
                "type": "TERM_CONFLICT", "mention": mention,
                "candidates": metric_terms,
                "reason": f"“{mention}”同时是多个指标的别名",
            })

    # 2/3. TIME_MISSING：无时间表达且数据集无默认时间 → 追问；有默认 → 不算歧义
    if intent.time_range is None:
        ds = snapshot.datasets.get(intent.dataset_id or "") or next(
            iter(snapshot.datasets.values()), None)
        if ds and not ds.get("default_time_range"):
            ambiguities.append({"type": "TIME_MISSING", "reason": "问题未给时间且数据集无默认时间"})

    # 4. GRAIN_MISSING：含"趋势/走势/变化"但无粒度
    if intent.grain is None and any(w in question for w in ("趋势", "走势", "变化")):
        ambiguities.append({"type": "GRAIN_MISSING", "reason": "含趋势类表达但未指定粒度"})

    # 5. VALUE_CONFLICT：mention 映射到多个不同技术值
    for mention in intent.mentions or []:
        hits = snapshot.resolve_dim_value(mention)
        techs = {h[1] for h in hits}
        if len(techs) >= 2:
            ambiguities.append({
                "type": "VALUE_CONFLICT", "mention": mention,
                "candidates": [f"{h[0]}:{h[1]}({h[2]})" for h in hits],
                "reason": f"“{mention}”可映射到多个维度取值",
            })

    # DATASET_CONFLICT：命中多个数据集（本项目单数据集，规则保留）
    ds_hits = {m["element_id"] for m in link_result.matched_elements
               if m["element_type"] == "dataset"}
    if len(ds_hits) >= 2:
        ambiguities.append({"type": "DATASET_CONFLICT", "candidates": sorted(ds_hits),
                            "reason": "问题涉及多个数据集"})

    # LLM 原始候选中未被规则覆盖的 TERM_CONFLICT 也采纳（复核 candidates 确实存在）
    for a in intent.ambiguities or []:
        if a.get("type") == "TERM_CONFLICT" and a.get("mention") not in term_hits:
            cands = [c for c in a.get("candidates", []) if c in snapshot.terms]
            if len(cands) >= 2 and not any(
                    x["type"] == "TERM_CONFLICT" and x["mention"] == a["mention"]
                    for x in ambiguities):
                ambiguities.append({**a, "candidates": cands})

    return ambiguities


# ---------- 卡片生成 ----------

def _owner_display(owner_id: str) -> str:
    try:
        conn = get_meta_conn()
        row = conn.execute("SELECT display_name, username FROM app_user WHERE id = ?",
                           (owner_id,)).fetchone()
        conn.close()
        if row:
            return row["display_name"] or row["username"]
    except Exception:  # noqa: BLE001
        pass
    return owner_id


def build_clarify(ambiguities: list[dict], snapshot: SemanticSnapshot,
                  round: int = 1) -> ClarifyCard | None:
    """把复核后的歧义生成澄清卡片（一次一张，按优先级取首个）。"""
    if not ambiguities or round > MAX_CLARIFY_ROUNDS:
        return None

    order = {"TERM_CONFLICT": 0, "VALUE_CONFLICT": 1, "DATASET_CONFLICT": 2,
             "TIME_MISSING": 3, "GRAIN_MISSING": 4}
    amb = sorted(ambiguities, key=lambda a: order.get(a["type"], 9))[0]
    clarify_id = f"cl_{uuid.uuid4().hex[:8]}"
    atype = amb["type"]

    if atype == "TERM_CONFLICT":
        options = []
        for tid in amb["candidates"]:
            t = snapshot.terms.get(tid)
            if not t:
                continue
            definition = (t.get("definition") or "")[:40]
            options.append({
                "key": tid,
                "label": prompts.CLARIFY_TERM_OPTION.format(
                    name=t["name"], definition=definition,
                    owner=_owner_display(t.get("owner", ""))),
            })
        if len(options) < 2:
            return None
        return ClarifyCard(clarify_id=clarify_id, type=atype,
                           prompt=prompts.CLARIFY_PROMPT_TERM_CONFLICT.format(
                               mention=amb["mention"]),
                           options=options, round=round)

    if atype == "TIME_MISSING":
        return ClarifyCard(clarify_id=clarify_id, type=atype,
                           prompt=prompts.CLARIFY_PROMPT_TIME_MISSING,
                           options=[{"key": k, "label": l} for k, l in
                                    zip(["last_7d", "last_month", "this_quarter", "custom"],
                                        prompts.CLARIFY_TIME_OPTIONS)],
                           round=round)

    if atype == "GRAIN_MISSING":
        return ClarifyCard(clarify_id=clarify_id, type=atype,
                           prompt=prompts.CLARIFY_PROMPT_GRAIN_MISSING,
                           options=[{"key": k, "label": l} for k, l in
                                    zip(["day", "week", "month"], prompts.CLARIFY_GRAIN_OPTIONS)],
                           round=round)

    if atype == "VALUE_CONFLICT":
        options = []
        for c in amb.get("candidates", []):
            dim_id, _, rest = c.partition(":")
            tech, _, disp = rest.partition("(")
            disp = disp.rstrip(")")
            options.append({"key": f"{dim_id}|{tech}", "label": f"{disp}（{tech}）"})
        if len(options) < 2:
            return None
        return ClarifyCard(clarify_id=clarify_id, type=atype,
                           prompt=prompts.CLARIFY_PROMPT_VALUE_CONFLICT.format(
                               mention=amb["mention"]),
                           options=options, round=round)

    if atype == "DATASET_CONFLICT":
        options = [{"key": d, "label": snapshot.datasets[d]["name"]}
                   for d in amb.get("candidates", []) if d in snapshot.datasets]
        if len(options) < 2:
            return None
        return ClarifyCard(clarify_id=clarify_id, type=atype,
                           prompt=prompts.CLARIFY_PROMPT_DATASET_CONFLICT,
                           options=options, round=round)

    return None
