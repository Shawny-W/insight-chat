"""语义层读取：SemanticSnapshot 内存快照 + 版本管理。

查询链路只读 published 版本；快照一次性全量加载（本地演示对象量为几十条，内存无压力），
含别名索引与维度值查找，供 schema linking / 编译 / 归因使用。
"""

import json
import sqlite3
from dataclasses import dataclass, field

# ---------- JSON 字段反序列化 ----------

_JSON_LIST_FIELDS = {
    "aliases", "related_terms", "trigger_keywords", "evidence_ids", "to_ids",
    "primary_key", "joins_needed", "grain", "related_dimensions", "value_maps",
    "hierarchy_path", "model_ids", "metric_ids", "dimension_ids", "allowed_values",
}
_JSON_DICT_FIELDS = {"format", "data_format", "extra", "value_domain", "options", "check_report"}


def _row_to_dict(row: sqlite3.Row) -> dict:
    obj = dict(row)
    for key, value in obj.items():
        if value is None or not isinstance(value, str):
            continue
        if key in _JSON_LIST_FIELDS or key in _JSON_DICT_FIELDS:
            try:
                obj[key] = json.loads(value)
            except json.JSONDecodeError:
                pass
    return obj


def _fetch_all(conn: sqlite3.Connection, table: str, version: int) -> list[dict]:
    rows = conn.execute(f"SELECT * FROM {table} WHERE version = ?", (version,)).fetchall()
    return [_row_to_dict(r) for r in rows]


# ---------- 快照 ----------


@dataclass
class SemanticSnapshot:
    """published 版本的全量语义对象内存镜像。"""

    version: int
    datasets: dict[str, dict] = field(default_factory=dict)
    models: dict[str, dict] = field(default_factory=dict)
    columns: list[dict] = field(default_factory=list)
    measures: list[dict] = field(default_factory=list)
    dimensions: dict[str, dict] = field(default_factory=dict)
    relationships: list[dict] = field(default_factory=list)
    metrics: dict[str, dict] = field(default_factory=dict)
    terms: dict[str, dict] = field(default_factory=dict)
    entities: dict[str, dict] = field(default_factory=dict)
    attributes: dict[str, dict] = field(default_factory=dict)
    relations: list[dict] = field(default_factory=list)
    rules: dict[str, dict] = field(default_factory=dict)
    evidences: dict[str, dict] = field(default_factory=dict)
    mappings: list[dict] = field(default_factory=list)
    # 别名索引：词(小写) -> [(element_type, element_id)]；element_type ∈ term/entity/attribute/metric/dimension/dataset/model
    alias_index: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    # 维度值索引：词 -> [(dimension_id, tech_value, display_value)]
    value_index: dict[str, list[tuple[str, str, str]]] = field(default_factory=dict)

    # ---------- 查询辅助 ----------

    def metric_of_term(self, term_id: str) -> dict | None:
        """metric_term → 绑定的 Metric（经 ont_mdl_mapping）。"""
        for m in self.mappings:
            if m["mapping_type"] == "term_metric" and m["ont_id"] == term_id:
                return self.metrics.get(m["mdl_id"])
        return None

    def term_of_metric(self, metric_id: str) -> dict | None:
        term = next((t for t in self.terms.values()
                     if self.metric_of_term(t["id"]) and self.metric_of_term(t["id"])["id"] == metric_id), None)
        if term:
            return term
        for m in self.metrics.values():
            if m["id"] == metric_id and m.get("bind_term_id"):
                return self.terms.get(m["bind_term_id"])
        return None

    def rules_of_term(self, term_id: str) -> list[dict]:
        """某 Term 关联的业务规则（target_id 命中）。"""
        return [r for r in self.rules.values() if r["target_id"] == term_id]

    def resolve_dim_value(self, word: str) -> list[tuple[str, str, str]]:
        """展示名/别名/技术值 → [(dimension_id, tech_value, display_value)]。"""
        return self.value_index.get(word, [])

    def lookup_alias(self, word: str) -> list[tuple[str, str]]:
        return self.alias_index.get(word, [])

    def summary(self) -> str:
        lines = [
            f"语义层快照 version=v{self.version}（published）",
            f"  DataSet 数据集      : {len(self.datasets)}",
            f"  Model 模型          : {len(self.models)}",
            f"  Relationship 关系   : {len(self.relationships)}",
            f"  Metric 指标         : {len(self.metrics)}",
            f"  Dimension 维度      : {len(self.dimensions)}",
            f"  Term 术语           : {len(self.terms)}",
            f"  Entity 实体         : {len(self.entities)}",
            f"  Attribute 属性      : {len(self.attributes)}",
            f"  Relation 语义关系   : {len(self.relations)}",
            f"  BusinessRule 规则   : {len(self.rules)}",
            f"  Evidence 证据       : {len(self.evidences)}",
            f"  Mapping 映射        : {len(self.mappings)}",
            f"  别名索引词条        : {len(self.alias_index)}",
            f"  维度值索引词条      : {len(self.value_index)}",
        ]
        return "\n".join(lines)


# ---------- 构建别名/值索引 ----------

def _add_alias(snap: SemanticSnapshot, word: str, element_type: str, element_id: str) -> None:
    word = (word or "").strip()
    if not word:
        return
    snap.alias_index.setdefault(word, [])
    entry = (element_type, element_id)
    if entry not in snap.alias_index[word]:
        snap.alias_index[word].append(entry)


def _build_indexes(snap: SemanticSnapshot) -> None:
    for t in snap.terms.values():
        _add_alias(snap, t["name"], "term", t["id"])
        for a in t.get("aliases", []):
            _add_alias(snap, a, "term", t["id"])
    for e in snap.entities.values():
        _add_alias(snap, e["name"], "entity", e["id"])
        for a in e.get("aliases", []):
            _add_alias(snap, a, "entity", e["id"])
    for a in snap.attributes.values():
        _add_alias(snap, a["name"], "attribute", a["id"])
        for al in a.get("aliases", []):
            _add_alias(snap, al, "attribute", a["id"])
    for m in snap.metrics.values():
        term = snap.term_of_metric(m["id"])
        if term:  # 指标以绑定 Term 的名称/别名入库（避免与 term 词条重复）
            continue
        _add_alias(snap, m["id"], "metric", m["id"])
    for d in snap.dimensions.values():
        _add_alias(snap, d["display_name"], "dimension", d["id"])
        for a in d.get("aliases", []):
            _add_alias(snap, a, "dimension", d["id"])
        for vm in d.get("value_maps", []):
            words = [vm.get("tech_value"), vm.get("display_value"), *vm.get("query_aliases", [])]
            for w in words:
                w = (w or "").strip()
                if not w:
                    continue
                entry = (d["id"], vm["tech_value"], vm["display_value"])
                snap.value_index.setdefault(w, [])
                if entry not in snap.value_index[w]:
                    snap.value_index[w].append(entry)
    for ds in snap.datasets.values():
        _add_alias(snap, ds["name"], "dataset", ds["id"])
    for mo in snap.models.values():
        _add_alias(snap, mo["name"], "model", mo["id"])


# ---------- 版本与加载 ----------

def get_published_version(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT version FROM semantic_version WHERE status = 'published'"
    ).fetchone()
    return row["version"] if row else None


def load_snapshot(conn: sqlite3.Connection | None = None,
                  version: int | None = None) -> SemanticSnapshot:
    """加载指定版本（默认当前 published）的全量语义对象为内存快照。"""
    own_conn = conn is None
    if own_conn:
        from chatbi.db import get_meta_conn
        conn = get_meta_conn()
    try:
        if version is None:
            version = get_published_version(conn)
            if version is None:
                raise RuntimeError("当前没有 published 的语义层版本，请先运行 scripts/init_db.py")

        snap = SemanticSnapshot(version=version)
        snap.datasets = {d["id"]: d for d in _fetch_all(conn, "mdl_dataset", version)}
        snap.models = {d["id"]: d for d in _fetch_all(conn, "mdl_model", version)}
        snap.columns = _fetch_all(conn, "mdl_column", version)
        snap.measures = _fetch_all(conn, "mdl_measure", version)
        snap.dimensions = {d["id"]: d for d in _fetch_all(conn, "mdl_dimension", version)}
        snap.relationships = _fetch_all(conn, "mdl_relationship", version)
        snap.metrics = {d["id"]: d for d in _fetch_all(conn, "mdl_metric", version)}
        snap.terms = {d["id"]: d for d in _fetch_all(conn, "ont_term", version)}
        snap.entities = {d["id"]: d for d in _fetch_all(conn, "ont_entity", version)}
        snap.attributes = {d["id"]: d for d in _fetch_all(conn, "ont_attribute", version)}
        snap.relations = _fetch_all(conn, "ont_relation", version)
        snap.rules = {d["id"]: d for d in _fetch_all(conn, "ont_business_rule", version)}
        snap.evidences = {d["id"]: d for d in _fetch_all(conn, "ont_evidence", version)}
        snap.mappings = _fetch_all(conn, "ont_mdl_mapping", version)
        _build_indexes(snap)
        return snap
    finally:
        if own_conn:
            conn.close()


# ---------- 幂等发布 ----------

def publish_seed(conn: sqlite3.Connection, payload: dict, changelog: str = "初始化种子（销售域）v1") -> int:
    """把种子负载发布为 v1 并置为 published；幂等：已有 published 版本则直接返回其版本号。

    payload 结构（见 semantic/seed.py 的组装）：
      {table_name: [row_dict, ...], ...}  —— 语义层对象表（ont_*/mdl_*/ont_mdl_mapping）
    版本无关对象（用户/角色/策略/事件/词条/示例）由 seed.py 单独导入，不在此函数职责内。
    """
    existing = get_published_version(conn)
    if existing is not None:
        return existing

    conn.execute(
        "INSERT INTO semantic_version (version, status, changelog, published_at) "
        "VALUES (1, 'published', ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
        (changelog,),
    )
    for table, rows in payload.items():
        # 仅给确实有 status 列的表补默认 status（mdl_column/mdl_measure 无此列）
        table_cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for row in rows:
            row = {**row, "version": 1}
            if "status" in table_cols:
                row.setdefault("status", "published")
            row = {k: v for k, v in row.items() if k in table_cols}
            for key, value in row.items():
                if isinstance(value, (list, dict)):
                    row[key] = json.dumps(value, ensure_ascii=False)
                elif isinstance(value, bool):
                    row[key] = int(value)
            cols = ", ".join(row.keys())
            placeholders = ", ".join(f":{k}" for k in row.keys())
            conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({placeholders})", row)
    conn.commit()
    return 1
