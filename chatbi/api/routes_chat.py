"""对话路由：会话 CRUD + POST /chat/ask（SSE）+ POST /chat/clarify（SSE 续走）。

SSE 事件：intent / clarify / progress / result / error（PRD 6.1.4）。
"""

import json
import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from chatbi.api.deps import get_current_user, require_roles
from chatbi.services import history
from chatbi.services.chat_service import ask, clarify
from chatbi.api.deps import ApiError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["对话"])


class AskBody(BaseModel):
    question: str
    session_id: str | None = None
    client: str = "web"


class ClarifyBody(BaseModel):
    clarify_id: str
    selected_key: str | None = None
    free_text: str | None = None


class NewSessionBody(BaseModel):
    title: str | None = None


def _sse(event_gen) -> EventSourceResponse:
    def gen():
        try:
            for ev in event_gen:
                yield {"event": ev["event"],
                       "data": json.dumps(ev["data"], ensure_ascii=False, default=str)}
        except Exception as exc:  # noqa: BLE001 - SSE 通道内兜底 error 事件
            logger.exception("SSE 流异常")
            yield {"event": "error",
                   "data": json.dumps({"code": "INTERNAL",
                                       "message": f"系统开小差了，请稍后再试（{exc}）"},
                                      ensure_ascii=False)}
    # sep="\n": LF 换行输出（sse-starlette 默认 CRLF 会导致前端按 \n\n 切分事件失败）；
    # 该参数在响应级覆盖每条事件的分隔符（ensure_bytes 会重写 dict 里的 sep 键）
    return EventSourceResponse(gen(), sep="\n")


@router.post("/chat/sessions")
def new_session(body: NewSessionBody, user: dict = Depends(get_current_user)):
    sid = history.create_session(user["id"], body.title or "新会话")
    return {"code": 0, "data": {"session_id": sid, "title": body.title or "新会话"}}


@router.get("/chat/sessions")
def list_session(user: dict = Depends(get_current_user), limit: int = 20,
                 cursor: str | None = None):
    return {"code": 0, "data": history.list_sessions(user["id"], limit, cursor)}


@router.get("/chat/sessions/{session_id}")
def get_session(session_id: str, user: dict = Depends(get_current_user)):
    data = history.get_session(session_id, user["id"])
    if data is None:
        raise ApiError(1003, "您没有该操作的权限", 403)
    return {"code": 0, "data": data}


@router.delete("/chat/sessions/{session_id}")
def delete_session(session_id: str, user: dict = Depends(get_current_user)):
    from chatbi.db import get_meta_conn
    conn = get_meta_conn()
    try:
        s = conn.execute("SELECT user_id FROM chat_session WHERE id = ?",
                         (session_id,)).fetchone()
        if s is None:
            raise ApiError(1004, "会话不存在", 404)
        if s["user_id"] != user["id"] and "admin" not in user.get("roles", []):
            raise ApiError(1003, "您没有该操作的权限", 403)
        conn.execute("DELETE FROM chat_message WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM chat_session WHERE id = ?", (session_id,))
        conn.commit()
        return {"code": 0, "data": {"deleted": True}}
    finally:
        conn.close()


@router.post("/chat/ask")
def ask_sse(body: AskBody, user: dict = Depends(get_current_user)):
    if "viewer" in user.get("roles", []) and "analyst" not in user.get("roles", []) \
            and "admin" not in user.get("roles", []):
        raise ApiError(1003, "您没有该操作的权限", 403)
    return _sse(ask(user, body.session_id, body.question))


@router.post("/chat/clarify")
def clarify_sse(body: ClarifyBody, user: dict = Depends(get_current_user)):
    return _sse(clarify(user, body.clarify_id, body.selected_key, body.free_text))
