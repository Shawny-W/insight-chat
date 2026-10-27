"""用户记忆路由：本人偏好管理 + 图表切换学习（统一 /api/v1 响应包）。"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from chatbi.api.deps import ApiError, audit, get_current_user
from chatbi.semantic.store import load_snapshot
from chatbi.services import memory

router = APIRouter(tags=["用户记忆"])


class MemoryBody(BaseModel):
    category: str
    memory_key: str = Field(min_length=1, max_length=256)
    value: dict


class ChartSwitchBody(BaseModel):
    answer_id: str = Field(min_length=1, max_length=64)
    chart_type: str = Field(min_length=1, max_length=32)
    session_id: str | None = Field(default=None, max_length=64)


def _raise_api(exc: Exception) -> None:
    message = str(exc.args[0]) if exc.args else str(exc)
    if isinstance(exc, KeyError):
        raise ApiError(1004, message, 404)
    if isinstance(exc, PermissionError):
        raise ApiError(1003, message, 403)
    raise ApiError(1400, message, 400)


@router.get("/memories")
def list_user_memories(include_history: bool = False,
                       user: dict = Depends(get_current_user)):
    items = memory.list_memories(user["id"], include_history)
    return {"code": 0, "data": {"items": items, "count": len(items)}}


@router.post("/memories")
def save_user_memory(body: MemoryBody, user: dict = Depends(get_current_user)):
    if body.category not in ("chart_preference", "term_mapping"):
        raise ApiError(1400, "只能显式保存图表偏好或术语映射", 400)
    try:
        if body.category == "term_mapping":
            term_id = body.value.get("term_id")
            snapshot = load_snapshot()
            term = snapshot.terms.get(term_id)
            if not term or term.get("term_type") != "metric_term" \
                    or not snapshot.metric_of_term(term_id):
                raise ValueError("指标术语不存在或当前语义版本已失效")
        item = memory.remember_preference(
            user["id"], body.category, body.memory_key.strip(), body.value,
            source="explicit")
    except (KeyError, PermissionError, ValueError) as exc:
        _raise_api(exc)
    audit(user["id"], "memory_upsert", item["id"],
          {"category": body.category, "key": body.memory_key})
    return {"code": 0, "data": item, "message": "记忆已保存"}


@router.delete("/memories/{memory_id}")
def delete_user_memory(memory_id: str, user: dict = Depends(get_current_user)):
    try:
        memory.delete_memory(user["id"], memory_id)
    except (KeyError, PermissionError, ValueError) as exc:
        _raise_api(exc)
    audit(user["id"], "memory_delete", memory_id)
    return {"code": 0, "data": {"deleted": True}}


@router.delete("/memories")
def clear_user_memories(category: str | None = None,
                        user: dict = Depends(get_current_user)):
    if category and category not in ("chart_preference", "term_mapping", "query_exemplar"):
        raise ApiError(1400, "不支持的记忆类别", 400)
    count = memory.clear_memories(user["id"], category)
    audit(user["id"], "memory_clear", detail={"category": category, "count": count})
    return {"code": 0, "data": {"deleted": count}}


@router.post("/memories/chart-switch")
def switch_chart(body: ChartSwitchBody, user: dict = Depends(get_current_user)):
    try:
        result = memory.switch_answer_chart(
            user["id"], body.answer_id, body.chart_type,
            session_id=body.session_id, source="chart_switch")
    except (KeyError, PermissionError, ValueError) as exc:
        _raise_api(exc)
    audit(user["id"], "memory_chart_switch", body.answer_id,
          {"chart_type": body.chart_type, "memory_id": result["memory"]["id"]})
    return {"code": 0, "data": {
        "chart": result["chart"], "memory": result["memory"],
        "memory_trace": result["memory_trace"],
    }, "message": result["memory_trace"][0]}
