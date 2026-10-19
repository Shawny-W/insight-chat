"""查询服务编排：linking → generate → compile → gateway → execute，落 query_log。

本模块不做意图/澄清（属 chat 层）；入参可携带意图阶段产物（mentions/time_range/grain）。
"""

import json
import sqlite3
import time
import uuid

from chatbi.db import get_meta_conn
from chatbi.nlp.linking import LinkResult, link
from chatbi.query.compiler import CompileError
from chatbi.query.compiler import compile as compile_semql
from chatbi.query.executor import ResultSet, execute
from chatbi.query.gateway import SqlForbiddenError, check_and_rewrite_detail
from chatbi.query.generator import LLMGenerateFailedError, generate
from chatbi.query.semql import SemQL, TimeSpec
from chatbi.semantic.store import SemanticSnapshot, load_snapshot


def load_policies(conn: sqlite3.Connection, user_id: str) -> list[dict]:
    """加载用户生效策略（经用户角色并集）。"""
    rows = conn.execute(
        """
        SELECT p.* FROM data_policy p
        JOIN user_role ur ON ur.role_id = p.role_id
        WHERE ur.user_id = ? AND p.status = 'active'
        """,
        (user_id,),
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("allowed_values"), str):
            try:
                d["allowed_values"] = json.loads(d["allowed_values"])
            except json.JSONDecodeError:
                pass
        result.append(d)
    return result


def pick_exemplars(conn: sqlite3.Connection, question: str, dataset_id: str,
                   top: int = 3) -> list[dict]:
    """seed 问答对按字面重合度召回 top-N（同 dataset）。"""
    rows = conn.execute(
        "SELECT * FROM query_exemplar WHERE dataset_id = ? AND is_active = 1",
        (dataset_id,),
    ).fetchall()

    def score(row) -> int:
        q = row["nl_question"]
        return sum(1 for ch in set(question) if ch in q)

    ranked = sorted(rows, key=score, reverse=True)
    picked = []
    for r in ranked[:top]:
        d = dict(r)
        try:
            d["semql_json"] = json.loads(d["semql_json"])
        except (json.JSONDecodeError, TypeError):
            pass
        picked.append(d)
    return picked


def _post_fix_semql(semql: SemQL, link_result: LinkResult,
                    snapshot: SemanticSnapshot,
                    time_range: dict | None, grain: str | None) -> None:
    """生成后确定性修正：时间范围用意图解析结果覆盖；命中的 business_term 强制内联。

    grain 优先级：LLM 输出的 SemQL grain 优先（LLM 更懂粒度语义），intent 的 grain 仅作兜底。"""
    if time_range:
        field = semql.time.field if semql.time else None
        llm_grain = semql.time.grain if semql.time else None
        semql.time = TimeSpec(field=field,
                              range=f"{time_range['start']}/{time_range['end']}",
                              grain=llm_grain or grain)
    for m in link_result.matched_elements:
        if m["element_type"] != "term":
            continue
        t = snapshot.terms.get(m["element_id"])
        if t and t.get("term_type") == "business_term" \
                and m["element_id"] not in semql.business_terms:
            semql.business_terms.append(m["element_id"])


def _log_query(conn: sqlite3.Connection, *, user_id: str, session_id: str | None,
               question: str, link_result: LinkResult | None, semql: SemQL | None,
               final_sql: str | None, applied_rules: list[str],
               semantic_version: int | None, retry_count: int,
               status: str, error_code: str | None, elapsed_ms: int) -> str:
    qid = f"qlog_{uuid.uuid4().hex[:12]}"
    conn.execute(
        """
        INSERT INTO query_log (id, session_id, user_id, question, matched_elements,
                               semql_json, final_sql, applied_rules, semantic_version,
                               retry_count, status, error_code, elapsed_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (qid, session_id or "", user_id, question,
         json.dumps(link_result.matched_elements if link_result else [], ensure_ascii=False),
         semql.model_dump_json() if semql else None,
         final_sql,
         json.dumps(applied_rules, ensure_ascii=False),
         semantic_version, retry_count, status, error_code, elapsed_ms),
    )
    conn.commit()
    return qid


def run_query(question: str, user_id: str = "usr_analyst", *,
              mentions: list[str] | None = None,
              time_range: dict | None = None, grain: str | None = None,
              ctx_turns: list[dict] | None = None,
              session_id: str | None = None,
              snapshot: SemanticSnapshot | None = None) -> dict:
    """查询主链路；失败时落 query_log 后原样上抛异常。"""
    t0 = time.monotonic()
    conn = get_meta_conn()
    snapshot = snapshot or load_snapshot(conn)
    policies = load_policies(conn, user_id)
    link_result: LinkResult | None = None
    semql: SemQL | None = None
    final_sql = None
    retry_count = 0

    try:
        # 1. schema linking
        link_result = link(question, mentions or [], snapshot)

        # 2. SemQL 生成（三级重试链在 generate 内）
        exemplars = pick_exemplars(conn, question, next(iter(snapshot.datasets)))
        try:
            semql = generate(question, link_result, snapshot, exemplars, ctx_turns)
        except LLMGenerateFailedError as exc:
            retry_count = 3
            raise exc
        _post_fix_semql(semql, link_result, snapshot, time_range, grain)
        errs = semql.validate_against(snapshot)
        if errs:
            raise CompileError("SEMQL_INVALID_REF", "; ".join(errs))

        # 3. 编译（SemQL 编译错误回显重试一次生成：PRD 6.3.6 可修复路径）
        try:
            compiled = compile_semql(semql, snapshot)
        except CompileError as exc:
            if exc.code in ("SEMQL_INVALID_VALUE", "SEMQL_INVALID_REF"):
                retry_count += 1
                semql = generate(question, link_result, snapshot, exemplars,
                                 ctx_turns, errors=[exc.message])
                _post_fix_semql(semql, link_result, snapshot, time_range, grain)
                compiled = compile_semql(semql, snapshot)
            else:
                raise

        # 4. 安全网关
        final_sql, gateway_notes = check_and_rewrite_detail(compiled.sql, policies, snapshot)

        # 5. 执行
        rs: ResultSet = execute(final_sql)

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        qid = _log_query(conn, user_id=user_id, session_id=session_id,
                         question=question, link_result=link_result, semql=semql,
                         final_sql=final_sql,
                         applied_rules=compiled.applied_rules + gateway_notes,
                         semantic_version=snapshot.version, retry_count=retry_count,
                         status="success", error_code=None, elapsed_ms=elapsed_ms)
        return {
            "query_log_id": qid,
            "semql": json.loads(semql.model_dump_json()),
            "sql": final_sql,
            "applied_rules": compiled.applied_rules + gateway_notes,
            "columns": rs.columns,
            "rows": [list(r) for r in rs.rows],
            "row_count": rs.row_count,
            "truncated": rs.truncated,
            "chart": None,  # 图表选择在后续阶段（chart/rules）
            "trace": compiled.trace,
            "assumptions": [],
            "degraded": [],
            "semantic_version": snapshot.version,
            "matched_elements": link_result.matched_elements,
            "elapsed_ms": elapsed_ms,
        }
    except (CompileError, SqlForbiddenError, LLMGenerateFailedError) as exc:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        code = getattr(exc, "code", "LLM_GENERATE_FAILED")
        _log_query(conn, user_id=user_id, session_id=session_id, question=question,
                   link_result=link_result, semql=semql, final_sql=final_sql,
                   applied_rules=[], semantic_version=snapshot.version,
                   retry_count=retry_count, status="failed",
                   error_code=code, elapsed_ms=elapsed_ms)
        raise
    finally:
        conn.close()
