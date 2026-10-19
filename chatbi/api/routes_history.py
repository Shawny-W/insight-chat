"""历史路由：GET /history/sessions（cursor 分页）、POST /history/answers/{id}/rerun。"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from chatbi.api.deps import ApiError, get_current_user
from chatbi.services import history

router = APIRouter(tags=["历史"])


@router.get("/history/sessions")
def sessions(user: dict = Depends(get_current_user), limit: int = 20,
             cursor: str | None = None):
    return {"code": 0, "data": history.list_sessions(user["id"], limit, cursor)}


class RerunBody(BaseModel):
    use_original_version: bool = False


@router.post("/history/answers/{answer_id}/rerun")
def rerun(answer_id: str, body: RerunBody, user: dict = Depends(get_current_user)):
    try:
        result = history.rerun_answer(answer_id, user["id"], body.use_original_version)
    except KeyError:
        raise ApiError(1004, "资源不存在", 404)
    except PermissionError:
        raise ApiError(1003, "您没有该操作的权限", 403)
    return {"code": 0, "data": result}
