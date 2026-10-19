"""认证路由：POST /auth/login、GET /auth/me。"""

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from chatbi.api.deps import ApiError, audit, get_current_user
from chatbi.services.auth import get_user_policies, login

router = APIRouter(tags=["认证"])


class LoginBody(BaseModel):
    username: str
    password: str


@router.post("/auth/login")
def do_login(body: LoginBody, request: Request):
    result = login(body.username, body.password)
    if result is None:
        audit("anonymous", "login_failed", body.username,
              ip=request.client.host if request.client else None)
        raise ApiError(1001, "用户名或密码错误", 401)
    audit(result["user"]["id"], "login", body.username,
          ip=request.client.host if request.client else None)
    return {"code": 0, "data": result}


@router.get("/auth/me")
def me(user: dict = Depends(get_current_user)):
    return {"code": 0, "data": {**user, "policies": get_user_policies(user["id"])}}
