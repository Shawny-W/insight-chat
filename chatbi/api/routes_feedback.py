"""反馈路由：POST /feedback、GET /feedback/queue（语义管理员/管理员）。"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from chatbi.api.deps import ApiError, get_current_user, require_roles
from chatbi.services import feedback

router = APIRouter(tags=["反馈"])


class FeedbackBody(BaseModel):
    answer_id: str | None = None
    insight_id: str | None = None
    vote: str                      # up/down
    reason_tag: str | None = None
    comment: str | None = None


@router.post("/feedback")
def submit(body: FeedbackBody, user: dict = Depends(get_current_user)):
    try:
        result = feedback.submit(user["id"], body.vote, body.answer_id,
                                 body.insight_id, body.reason_tag, body.comment)
    except ValueError as exc:
        raise ApiError(1400, str(exc), 400)
    return {"code": 0, "data": result,
            "message": "感谢认可" if body.vote == "up" else "感谢反馈，我们会持续改进"}


@router.get("/feedback/queue")
def pending_queue(user: dict = Depends(require_roles("admin", "semantic_admin"))):
    return {"code": 0, "data": feedback.queue()}
