"""反馈回流服务：👍/👎 + 原因标签；👍 沉淀 NL→SemQL exemplar（3 人👍转正）；👎 进待办队列。"""

import json
import logging
import sqlite3
import uuid

from chatbi.db import get_meta_conn

logger = logging.getLogger(__name__)

REASON_TAGS = ("wrong_data", "wrong_intent", "bad_chart", "untrusted_insight", "other")
PROMOTE_UPVOTES = 3  # 同问题 3 个不同用户 👍 → exemplar 转正


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def submit(user_id: str, vote: str, answer_id: str | None = None,
           insight_id: str | None = None, reason_tag: str | None = None,
           comment: str | None = None) -> dict:
    """提交反馈；同用户同对象覆盖更新（feedback_history 记变更）。"""
    if vote not in ("up", "down"):
        raise ValueError("vote 只能是 up/down")
    if vote == "down" and reason_tag not in REASON_TAGS:
        raise ValueError(f"👎 必须选择原因标签: {REASON_TAGS}")
    if not answer_id and not insight_id:
        raise ValueError("answer_id 与 insight_id 至少提供一个")

    conn = get_meta_conn()
    try:
        existing = conn.execute(
            "SELECT * FROM feedback WHERE user_id = ? AND "
            "COALESCE(answer_id, '') = COALESCE(?, '') AND "
            "COALESCE(insight_id, '') = COALESCE(?, '')",
            (user_id, answer_id, insight_id)).fetchone()

        if existing:
            fid = existing["id"]
            conn.execute(
                "INSERT INTO feedback_history (feedback_id, old_vote, new_vote, old_tag, new_tag) "
                "VALUES (?, ?, ?, ?, ?)",
                (fid, existing["vote"], vote, existing["reason_tag"], reason_tag))
            conn.execute(
                "UPDATE feedback SET vote = ?, reason_tag = ?, comment = ?, status = 'pending', "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
                (vote, reason_tag, comment, fid))
        else:
            fid = _id("fb")
            conn.execute(
                "INSERT INTO feedback (id, answer_id, insight_id, user_id, vote, reason_tag, "
                "comment, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
                (fid, answer_id, insight_id, user_id, vote, reason_tag, comment))
        conn.commit()

        if vote == "up" and answer_id:
            _upvote_to_exemplar(conn, user_id, answer_id)
            conn.commit()
        # 个人问答对与原有全局 exemplar 分开沉淀；记忆故障不影响反馈提交
        if answer_id:
            try:
                from chatbi.services import memory
                if vote == "up":
                    memory.remember_exemplar(user_id, answer_id)
                else:
                    memory.forget_exemplar(user_id, answer_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("个人问答对记忆写入失败（反馈已保存）: %s", exc)
        return {"feedback_id": fid, "updated": existing is not None}
    finally:
        conn.close()


def _upvote_to_exemplar(conn: sqlite3.Connection, user_id: str, answer_id: str) -> None:
    """👍 → 对应 query 的 NL→SemQL 对进入 exemplar 候选；3 个不同用户👍 转正。"""
    row = conn.execute(
        """
        SELECT q.question, q.semql_json FROM answer a
        JOIN query_log q ON a.query_log_id = q.id
        WHERE a.id = ? AND q.semql_json IS NOT NULL
        """, (answer_id,)).fetchone()
    if row is None:
        return
    question, semql_json = row["question"], row["semql_json"]

    existing = conn.execute(
        "SELECT * FROM query_exemplar WHERE nl_question = ? AND source = 'user_feedback'",
        (question,)).fetchone()
    # 用 quality_score 记录不同用户👍数；hit_count 高位暂不维护
    up_users = conn.execute(
        """
        SELECT COUNT(DISTINCT f.user_id) AS n FROM feedback f
        JOIN answer a ON f.answer_id = a.id
        JOIN query_log q ON a.query_log_id = q.id
        WHERE f.vote = 'up' AND q.question = ?
        """, (question,)).fetchone()["n"]

    if existing:
        active = 1 if up_users >= PROMOTE_UPVOTES else existing["is_active"]
        conn.execute(
            "UPDATE query_exemplar SET quality_score = ?, is_active = ? WHERE id = ?",
            (up_users, active, existing["id"]))
    else:
        try:
            semql = json.loads(semql_json)
        except json.JSONDecodeError:
            return
        conn.execute(
            "INSERT INTO query_exemplar (id, dataset_id, nl_question, semql_json, source, "
            "quality_score, is_active) VALUES (?, ?, ?, ?, 'user_feedback', ?, ?)",
            (_id("ex"), semql.get("dataset", "ds_sales"), question,
             json.dumps(semql, ensure_ascii=False), up_users,
             1 if up_users >= PROMOTE_UPVOTES else 0))


def queue(status: str = "pending", limit: int = 50) -> list[dict]:
    """分析师待办队列（👎 反馈，时间倒序）。"""
    conn = get_meta_conn()
    try:
        rows = conn.execute(
            """
            SELECT f.*, q.question, q.final_sql FROM feedback f
            LEFT JOIN answer a ON f.answer_id = a.id
            LEFT JOIN query_log q ON a.query_log_id = q.id
            WHERE f.status = ? AND f.vote = 'down'
            ORDER BY f.created_at DESC LIMIT ?
            """, (status, limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
