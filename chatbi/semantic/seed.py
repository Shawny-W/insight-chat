"""种子导入：读取 seed/ontology_seed.yaml，导入 meta 库并发布为 v1。

职责划分：
- 版本无关对象（数据源/角色/用户/策略/事件/词条/示例）：幂等 upsert
- 语义层对象（ont_*/mdl_*/ont_mdl_mapping）：组装 payload 交 store.publish_seed（幂等）
- Evidence：对零售库真实执行 query_sql，回填 result_summary / validated_at
"""

import json
import sqlite3
from pathlib import Path

import yaml

from chatbi.security import hash_password

SEED_FILE = Path(__file__).parent / "seed" / "ontology_seed.yaml"


def load_seed_yaml(path: Path | None = None) -> dict:
    with open(path or SEED_FILE, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _upsert(conn: sqlite3.Connection, table: str, row: dict, key: str = "id") -> None:
    """按主键幂等插入（已存在则跳过）。"""
    row = dict(row)
    for k, v in row.items():
        if isinstance(v, (list, dict)):
            row[k] = json.dumps(v, ensure_ascii=False)
        elif isinstance(v, bool):
            row[k] = int(v)
    exists = conn.execute(
        f"SELECT 1 FROM {table} WHERE {key} = ?", (row[key],)
    ).fetchone()
    if exists:
        return
    cols = ", ".join(row.keys())
    placeholders = ", ".join(f":{k}" for k in row.keys())
    conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({placeholders})", row)


def seed_foundation(conn: sqlite3.Connection, seed: dict) -> dict[str, int]:
    """导入版本无关的基础数据（幂等）。返回各类对象的导入计数。"""
    counts: dict[str, int] = {}

    _upsert(conn, "datasource", seed["datasource"])
    counts["datasource"] = 1

    for role in seed["roles"]:
        _upsert(conn, "role", role)
    counts["role"] = len(seed["roles"])

    for user in seed["users"]:
        row = {k: v for k, v in user.items() if k != "password"}
        row["password_hash"] = hash_password(user["password"])
        _upsert(conn, "app_user", row)
    counts["app_user"] = len(seed["users"])

    # user_role 为联合主键，直接 INSERT OR IGNORE 实现幂等
    for ur in seed["user_roles"]:
        conn.execute(
            "INSERT OR IGNORE INTO user_role (user_id, role_id) VALUES (?, ?)",
            (ur["user_id"], ur["role_id"]),
        )
    counts["user_role"] = len(seed["user_roles"])

    for pol in seed["policies"]:
        _upsert(conn, "data_policy", pol)
    counts["data_policy"] = len(seed["policies"])

    for evt in seed["business_events"]:
        _upsert(conn, "business_event", {**evt, "created_by": "usr_admin"})
    counts["business_event"] = len(seed["business_events"])

    for de in seed["dict_entries"]:
        _upsert(conn, "dict_entry", de)
    counts["dict_entry"] = len(seed["dict_entries"])

    for ex in seed["exemplars"]:
        _upsert(conn, "query_exemplar", ex)
    counts["query_exemplar"] = len(seed["exemplars"])

    conn.commit()
    return counts


def build_semantic_payload(seed: dict) -> dict[str, list[dict]]:
    """把 yaml 中的语义层对象组装为 publish_seed 的 payload（表名 → 行列表）。"""
    payload: dict[str, list[dict]] = {}

    payload["mdl_model"] = [
        {k: v for k, v in m.items()} for m in seed["models"]
    ]
    for m in payload["mdl_model"]:
        m["datasource_id"] = seed["datasource"]["id"]

    payload["mdl_column"] = [dict(c) for c in seed["columns"]]
    payload["mdl_relationship"] = [dict(r) for r in seed["relationships"]]
    payload["mdl_metric"] = [dict(m) for m in seed["metrics"]]
    payload["mdl_dimension"] = [dict(d) for d in seed["dimensions"]]
    payload["mdl_dataset"] = [dict(d) for d in seed["datasets"]]

    payload["ont_term"] = [dict(t) for t in seed["terms"]]
    payload["ont_entity"] = [dict(e) for e in seed["entities"]]
    payload["ont_attribute"] = [dict(a) for a in seed["attributes"]]
    payload["ont_relation"] = [dict(r) for r in seed["relations"]]
    payload["ont_business_rule"] = [dict(r) for r in seed["rules"]]
    payload["ont_mdl_mapping"] = [dict(m) for m in seed["mappings"]]

    # Evidence 先占位（result_summary 由 run_evidences 真实执行后回填）
    payload["ont_evidence"] = [
        {
            "id": ev["id"],
            "source_desc": ev["source_desc"],
            "query_sql": ev["query_sql"].strip(),
            "result_summary": "（待执行）",
            "validated_at": "",
            "validated_by": "usr_analyst",
        }
        for ev in seed["evidences"]
    ]
    return payload


def run_evidences(conn: sqlite3.Connection, biz_conn: sqlite3.Connection,
                  seed: dict, version: int) -> list[dict]:
    """对业务库真实执行每条 Evidence 的 query_sql，回填结果（幂等：重复执行覆盖结果）。"""
    results = []
    for ev in seed["evidences"]:
        sql = ev["query_sql"].strip()
        try:
            rows = biz_conn.execute(sql).fetchall()
            if len(rows) == 1 and len(rows[0].keys()) == 1:
                result_summary = f"{ev['label']} = {rows[0][0]}"
            else:
                result_summary = "；".join(
                    ", ".join(f"{k}={r[k]}" for k in r.keys()) for r in rows
                )
            error = None
        except Exception as exc:  # noqa: BLE001
            result_summary = f"执行失败: {exc}"
            error = exc
        conn.execute(
            "UPDATE ont_evidence SET result_summary = ?, "
            "validated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE id = ? AND version = ?",
            (result_summary, ev["id"], version),
        )
        results.append({"id": ev["id"], "label": ev["label"],
                        "result": result_summary, "error": str(error) if error else None})
    conn.commit()
    return results


def run_seed(conn: sqlite3.Connection, biz_conn: sqlite3.Connection) -> dict:
    """完整种子流程：基础数据 → 语义层发布 v1 → Evidence 真实执行。幂等。"""
    seed = load_seed_yaml()
    counts = seed_foundation(conn, seed)

    from chatbi.semantic.store import get_published_version, publish_seed

    already_published = get_published_version(conn) is not None
    payload = build_semantic_payload(seed)
    version = publish_seed(conn, payload)

    evidence_results = run_evidences(conn, biz_conn, seed, version)

    return {
        "version": version,
        "already_published": already_published,
        "foundation": counts,
        "semantic_counts": {table: len(rows) for table, rows in payload.items()},
        "evidences": evidence_results,
    }
