"""API 依赖：token 解析、角色守卫、审计日志、统一错误类型（PRD 8.1/8.2）。"""

import uuid

from fastapi import Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from chatbi.db import get_meta_conn
from chatbi.services.auth import current_user

_bearer = HTTPBearer(auto_error=False)


class ApiError(Exception):
    """统一业务错误：{"code":..., "message":..., "trace_id":...}。"""

    def __init__(self, code: int, message: str, http_status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


def get_current_user(request: Request) -> dict:
    """从 Authorization: Bearer <token> 解析当前用户；失败 1001。"""
    creds: HTTPAuthorizationCredentials | None = request.scope.get("bearer_credentials")
    if creds is None:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
        else:
            raise ApiError(1001, "登录已过期，请重新登录", 401)
    else:
        token = creds.credentials
    user = current_user(token)
    if user is None:
        raise ApiError(1001, "登录已过期，请重新登录", 401)
    request.state.user = user
    return user


def require_roles(*roles: str):
    """角色守卫：admin 角色恒放行。"""
    def checker(request: Request) -> dict:
        user = get_current_user(request)
        user_roles = set(user.get("roles", []))
        if "admin" in user_roles or user_roles & set(roles):
            return user
        raise ApiError(1003, "您没有该操作的权限", 403)
    return checker


def audit(user_id: str, action: str, target: str | None = None,
          detail: dict | None = None, ip: str | None = None) -> None:
    """审计日志（敏感操作调用方主动记录）。"""
    import json
    conn = get_meta_conn()
    try:
        conn.execute(
            "INSERT INTO audit_log (user_id, action, target, detail, ip) VALUES (?,?,?,?,?)",
            (user_id, action, target,
             json.dumps(detail, ensure_ascii=False) if detail else None, ip))
        conn.commit()
    finally:
        conn.close()
