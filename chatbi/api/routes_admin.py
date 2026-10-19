"""管理路由（只读为主）：语义层对象浏览、linking 调试台、用户/审计查看。"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from chatbi.api.deps import get_current_user, require_roles
from chatbi.db import get_meta_conn
from chatbi.nlp.linking import link
from chatbi.semantic.store import load_snapshot

router = APIRouter(tags=["管理"])


@router.get("/semantic/summary")
def semantic_summary(user: dict = Depends(require_roles("admin", "semantic_admin", "analyst"))):
    snap = load_snapshot()
    return {"code": 0, "data": {
        "version": snap.version,
        "counts": {
            "datasets": len(snap.datasets), "models": len(snap.models),
            "relationships": len(snap.relationships), "metrics": len(snap.metrics),
            "dimensions": len(snap.dimensions), "terms": len(snap.terms),
            "entities": len(snap.entities), "attributes": len(snap.attributes),
            "relations": len(snap.relations), "rules": len(snap.rules),
            "evidences": len(snap.evidences), "mappings": len(snap.mappings),
        },
        "alias_index_size": len(snap.alias_index),
        "value_index_size": len(snap.value_index),
    }}


@router.get("/semantic/terms")
def list_terms(user: dict = Depends(require_roles("admin", "semantic_admin"))):
    snap = load_snapshot()
    return {"code": 0, "data": list(snap.terms.values())}


@router.get("/semantic/metrics")
def list_metrics(user: dict = Depends(require_roles("admin", "semantic_admin"))):
    snap = load_snapshot()
    return {"code": 0, "data": list(snap.metrics.values())}


@router.get("/semantic/dimensions")
def list_dimensions(user: dict = Depends(require_roles("admin", "semantic_admin"))):
    snap = load_snapshot()
    return {"code": 0, "data": list(snap.dimensions.values())}


@router.get("/semantic/rules")
def list_rules(user: dict = Depends(require_roles("admin", "semantic_admin"))):
    snap = load_snapshot()
    return {"code": 0, "data": list(snap.rules.values())}


@router.get("/semantic/events")
def list_events(user: dict = Depends(require_roles("admin", "semantic_admin"))):
    conn = get_meta_conn()
    try:
        rows = conn.execute("SELECT * FROM business_event ORDER BY start_date DESC").fetchall()
        return {"code": 0, "data": [dict(r) for r in rows]}
    finally:
        conn.close()


class LinkBody(BaseModel):
    question: str
    dataset_id: str | None = None


@router.api_route("/semantic/debug/link", methods=["GET", "POST"])
def debug_link(request_body: LinkBody | None = None, question: str | None = None,
               user: dict = Depends(require_roles("admin", "semantic_admin"))):
    q = (request_body.question if request_body else question) or ""
    snap = load_snapshot()
    result = link(q, [], snap, (request_body.dataset_id if request_body else None))
    return {"code": 0, "data": {
        "matched_elements": result.matched_elements,
        "unmatched": result.unmatched,
        "candidates_dropped": result.candidates_dropped,
    }}


@router.get("/sys/users")
def list_users(user: dict = Depends(require_roles("admin"))):
    conn = get_meta_conn()
    try:
        rows = conn.execute(
            "SELECT id, username, display_name, dept, status, created_at FROM app_user"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            roles = conn.execute(
                "SELECT r.code FROM role r JOIN user_role ur ON ur.role_id = r.id "
                "WHERE ur.user_id = ?", (d["id"],)).fetchall()
            d["roles"] = [x["code"] for x in roles]
            out.append(d)
        return {"code": 0, "data": out}
    finally:
        conn.close()


@router.get("/sys/audit-logs")
def audit_logs(user: dict = Depends(require_roles("admin")), limit: int = 50):
    conn = get_meta_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return {"code": 0, "data": [dict(r) for r in rows]}
    finally:
        conn.close()
