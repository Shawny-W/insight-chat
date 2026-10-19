"""洞察路由：POST /insight/attribute（本地版同步执行）、GET /insight/{report_id}。"""

import json
import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from chatbi.api.deps import ApiError, get_current_user
from chatbi.db import get_meta_conn
from chatbi.insight.attribution import AttributionError, attribute
from chatbi.insight.narrative import narrate
from chatbi.semantic.store import load_snapshot
from chatbi.services.auth import get_user_policies

router = APIRouter(tags=["洞察"])


class AttributeBody(BaseModel):
    metric_id: str
    period_a: str
    period_b: str
    base_filters: list[dict] | None = None
    session_id: str | None = None


@router.post("/insight/attribute")
def do_attribute(body: AttributeBody, user: dict = Depends(get_current_user)):
    snapshot = load_snapshot()
    policies = get_user_policies(user["id"])
    flat = policies["row_policies"] + policies["column_policies"]
    base_filters = body.base_filters or [
        {"dimension": "dim_channel", "op": "not_in", "values": ["TEST"]}]
    try:
        result = attribute(body.metric_id, body.period_a, body.period_b,
                           base_filters, snapshot, flat)
    except AttributionError as exc:
        code = {"ATTRIB_NO_DIMENSIONS": 3001, "ATTRIB_NO_METRIC": 3001}.get(exc.code, 3004)
        raise ApiError(code, exc.message, 400)
    report = result.to_dict()
    if not result.is_significant:
        # 3003：波动不显著（非错误的业务返回）
        narrative = None
    else:
        narrative, degraded = narrate(report)
        report["narrative_degraded"] = degraded

    rid = f"attr_{uuid.uuid4().hex[:12]}"
    conn = get_meta_conn()
    try:
        conn.execute(
            "INSERT INTO insight_report (id, user_id, session_id, metric_id, period_a, "
            "period_b, status, report_json, narrative, semantic_version) "
            "VALUES (?, ?, ?, ?, ?, ?, 'completed', ?, ?, ?)",
            (rid, user["id"], body.session_id, body.metric_id,
             body.period_a, body.period_b,
             json.dumps(report, ensure_ascii=False, default=str), narrative,
             snapshot.version))
        conn.commit()
    finally:
        conn.close()
    return {"code": 0, "data": {"report_id": rid, "report": report,
                                "narrative": narrative}}


@router.get("/insight/{report_id}")
def get_report(report_id: str, user: dict = Depends(get_current_user)):
    conn = get_meta_conn()
    try:
        row = conn.execute("SELECT * FROM insight_report WHERE id = ?",
                           (report_id,)).fetchone()
        if row is None:
            raise ApiError(1004, "归因报告不存在", 404)
        if row["user_id"] != user["id"] and "admin" not in user.get("roles", []):
            raise ApiError(1003, "您没有该操作的权限", 403)
        return {"code": 0, "data": {
            "report_id": report_id, "status": row["status"],
            "report": json.loads(row["report_json"]) if row["report_json"] else None,
            "narrative": row["narrative"],
            "semantic_version": row["semantic_version"],
            "created_at": row["created_at"]}}
    finally:
        conn.close()
