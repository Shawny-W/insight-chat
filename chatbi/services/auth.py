"""认证服务：账号密码登录 + token 签发 + 用户策略聚合。"""

import json
import sqlite3

from chatbi.db import get_meta_conn
from chatbi.security import hash_password, make_token, verify_password, verify_token


def get_user(user_id: str, conn: sqlite3.Connection | None = None) -> dict | None:
    own = conn is None
    conn = conn or get_meta_conn()
    try:
        row = conn.execute(
            "SELECT id, username, display_name, email, dept, status FROM app_user WHERE id = ?",
            (user_id,)).fetchone()
        return dict(row) if row else None
    finally:
        if own:
            conn.close()


def get_user_roles(user_id: str, conn: sqlite3.Connection | None = None) -> list[str]:
    own = conn is None
    conn = conn or get_meta_conn()
    try:
        rows = conn.execute(
            """
            SELECT r.code FROM role r
            JOIN user_role ur ON ur.role_id = r.id
            WHERE ur.user_id = ?
            """, (user_id,)).fetchall()
        return [r["code"] for r in rows]
    finally:
        if own:
            conn.close()


def login(username: str, password: str) -> dict | None:
    """校验账号密码，成功返回 {token, user:{id,username,display_name,roles}}，失败 None。"""
    conn = get_meta_conn()
    try:
        row = conn.execute(
            "SELECT * FROM app_user WHERE username = ? AND status = 'active'",
            (username,)).fetchone()
        if row is None or not verify_password(password, row["password_hash"]):
            return None
        user = {"id": row["id"], "username": row["username"],
                "display_name": row["display_name"], "roles": get_user_roles(row["id"], conn)}
        return {"token": make_token(row["id"]), "user": user}
    finally:
        conn.close()


def current_user(token: str) -> dict | None:
    """token → 用户信息（含角色）；无效/过期返回 None。"""
    user_id = verify_token(token)
    if not user_id:
        return None
    user = get_user(user_id)
    if not user:
        return None
    user["roles"] = get_user_roles(user_id)
    return user


def get_user_policies(user_id: str) -> dict:
    """聚合用户全部角色的 data_policy：{roles, dataset_ids, row_policies, column_policies}。"""
    conn = get_meta_conn()
    try:
        roles = get_user_roles(user_id, conn)
        rows = conn.execute(
            """
            SELECT DISTINCT p.* FROM data_policy p
            JOIN user_role ur ON ur.role_id = p.role_id
            WHERE ur.user_id = ? AND p.status = 'active'
            """, (user_id,)).fetchall()
        dataset_ids, row_policies, column_policies = [], [], []
        for r in rows:
            d = dict(r)
            if isinstance(d.get("allowed_values"), str):
                try:
                    d["allowed_values"] = json.loads(d["allowed_values"])
                except json.JSONDecodeError:
                    pass
            if d["policy_type"] == "dataset":
                dataset_ids.append(d["dataset_id"])
            elif d["policy_type"] == "row":
                row_policies.append(d)
            elif d["policy_type"] == "column":
                column_policies.append(d)
        return {"roles": roles, "dataset_ids": sorted(set(dataset_ids)),
                "row_policies": row_policies, "column_policies": column_policies}
    finally:
        conn.close()
