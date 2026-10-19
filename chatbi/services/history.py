"""查询历史服务：会话/消息/answer 落库与查询、重跑（支持按原口径版本）。"""

import json
import sqlite3
import uuid

from chatbi.db import get_meta_conn


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def create_session(user_id: str, title: str | None = None,
                   conn: sqlite3.Connection | None = None) -> str:
    own = conn is None
    conn = conn or get_meta_conn()
    try:
        sid = _id("sess")
        conn.execute("INSERT INTO chat_session (id, user_id, title) VALUES (?, ?, ?)",
                     (sid, user_id, title))
        conn.commit()
        return sid
    finally:
        if own:
            conn.close()


def save_message(session_id: str, role: str, content: str, intent: str | None = None,
                 clarify_info: dict | None = None,
                 conn: sqlite3.Connection | None = None) -> str:
    own = conn is None
    conn = conn or get_meta_conn()
    try:
        mid = _id("msg")
        conn.execute(
            "INSERT INTO chat_message (id, session_id, role, content, intent, clarify_info) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (mid, session_id, role, content, intent,
             json.dumps(clarify_info, ensure_ascii=False) if clarify_info else None))
        conn.execute("UPDATE chat_session SET "
                     "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
                     (session_id,))
        conn.commit()
        return mid
    finally:
        if own:
            conn.close()


def recent_messages(session_id: str, limit: int = 10,
                    conn: sqlite3.Connection | None = None) -> list[dict]:
    own = conn is None
    conn = conn or get_meta_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM chat_message WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
            (session_id, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]
    finally:
        if own:
            conn.close()


def list_sessions(user_id: str, limit: int = 20, cursor: str | None = None,
                  conn: sqlite3.Connection | None = None) -> dict:
    own = conn is None
    conn = conn or get_meta_conn()
    try:
        sql = ("SELECT * FROM chat_session WHERE user_id = ?")
        params: list = [user_id]
        if cursor:
            sql += " AND updated_at < ?"
            params.append(cursor)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit + 1)
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        next_cursor = None
        if len(rows) > limit:
            rows = rows[:limit]
            next_cursor = rows[-1]["updated_at"]
        return {"items": rows, "next_cursor": next_cursor}
    finally:
        if own:
            conn.close()


def get_session(session_id: str, user_id: str,
                conn: sqlite3.Connection | None = None) -> dict | None:
    own = conn is None
    conn = conn or get_meta_conn()
    try:
        s = conn.execute("SELECT * FROM chat_session WHERE id = ?", (session_id,)).fetchone()
        if s is None:
            return None
        if s["user_id"] != user_id:
            return None  # 越权：只能看自己的历史
        return {"session": dict(s), "messages": recent_messages(session_id, 100, conn)}
    finally:
        if own:
            conn.close()


def save_answer(query_log_id: str, chart_type: str | None, chart_meta: dict | None,
                columns_meta: list | None, result_snapshot: dict | None,
                row_count: int, summary_text: str | None,
                conn: sqlite3.Connection | None = None) -> str:
    own = conn is None
    conn = conn or get_meta_conn()
    try:
        aid = _id("ans")
        conn.execute(
            "INSERT INTO answer (id, query_log_id, chart_type, chart_meta, columns_meta, "
            "result_snapshot, row_count, summary_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (aid, query_log_id, chart_type,
             json.dumps(chart_meta, ensure_ascii=False) if chart_meta else None,
             json.dumps(columns_meta, ensure_ascii=False) if columns_meta else None,
             json.dumps(result_snapshot, ensure_ascii=False, default=str)
             if result_snapshot else None,
             row_count, summary_text))
        conn.commit()
        return aid
    finally:
        if own:
            conn.close()


def rerun_answer(answer_id: str, user_id: str,
                 use_original_version: bool = False) -> dict:
    """重跑历史 answer：默认按当前 published 版本；use_original_version=True 按原口径版本。"""
    from chatbi.semantic.store import load_snapshot
    from chatbi.services.query_service import run_query

    conn = get_meta_conn()
    try:
        row = conn.execute(
            """
            SELECT a.id AS answer_id, q.* FROM answer a
            JOIN query_log q ON a.query_log_id = q.id
            WHERE a.id = ?
            """, (answer_id,)).fetchone()
        if row is None:
            raise KeyError(f"answer {answer_id} 不存在")
        row = dict(row)
        if row["user_id"] != user_id:
            raise PermissionError("只能重跑自己的历史查询")
        orig_version = row["semantic_version"]
        snapshot = load_snapshot(conn, version=orig_version) \
            if use_original_version and orig_version else load_snapshot(conn)
        result = run_query(row["question"], user_id=user_id,
                           session_id=row["session_id"], snapshot=snapshot)
        result["rerun_of"] = answer_id
        result["original_semantic_version"] = orig_version
        if not use_original_version and snapshot.version != orig_version:
            result["version_note"] = (
                f"当前口径(v{snapshot.version})与原始查询时口径(v{orig_version})可能不同")
        return result
    finally:
        conn.close()
