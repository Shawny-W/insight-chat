"""用户记忆系统测试（全部使用独立内存 SQLite，不依赖 LLM、不写真实 meta.db）。"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from chatbi.nlp.linking import LinkResult
from chatbi.semantic.schema import init_schema
from chatbi.services import memory
from chatbi.services import chat_service
from chatbi.services.query_service import pick_exemplars


@pytest.fixture()
def conn():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    init_schema(db)
    for uid in ("u1", "u2"):
        db.execute(
            "INSERT INTO app_user (id, username, display_name, password_hash) "
            "VALUES (?, ?, '测试用户', 'unused')", (uid, uid))
    db.commit()
    yield db
    db.close()


@pytest.fixture()
def snap():
    terms = {
        "term_net_sales": {"name": "净销售额", "term_type": "metric_term", "aliases": ["销售额"]},
        "term_gross_sales": {"name": "毛销售额", "term_type": "metric_term", "aliases": ["销售额"]},
    }
    metrics = {"term_net_sales": "metric_net_sales", "term_gross_sales": "metric_gross_sales"}
    return SimpleNamespace(terms=terms, metric_of_term=lambda term_id: (
        {"id": metrics[term_id]} if term_id in metrics else None))


def _session(conn, sid="s1", uid="u1"):
    conn.execute("INSERT INTO chat_session (id, user_id) VALUES (?, ?)", (sid, uid))
    conn.commit()


def _answer(conn, aid="a1", uid="u1", sid="s1", snapshot=True):
    _session(conn, sid, uid)
    semql = {"dataset": "ds_sales", "metrics": ["metric_net_sales"], "dimensions": [],
             "time": {"field": "orders.order_date", "range": "2024-03-01/2024-05-31",
                      "grain": "month"},
             "filters": [], "order_by": [], "limit": 100, "compare": None,
             "query_mode": "aggregate", "business_terms": [], "semql_version": "1.0"}
    conn.execute(
        "INSERT INTO query_log (id, session_id, user_id, question, semql_json, final_sql, "
        "applied_rules, semantic_version, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'success')",
        ("q_" + aid, sid, uid, "各月净销售额", json.dumps(semql, ensure_ascii=False),
         "SELECT 1", "[]", 1))
    result = {"columns": ["月份", "净销售额"], "rows": [["2024-04", 100], ["2024-05", 120]]} \
        if snapshot else None
    conn.execute(
        "INSERT INTO answer (id, query_log_id, chart_type, chart_meta, columns_meta, "
        "result_snapshot, row_count, summary_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (aid, "q_" + aid, "line", json.dumps(
            {"type": "line", "reason": "rule_2", "available_types": ["line", "bar", "table"]}),
         json.dumps([{"name": "月份", "role": "time"}, {"name": "净销售额", "role": "metric"}]),
         json.dumps(result, ensure_ascii=False) if result else None, 7, "测试摘要"))
    conn.commit()
    return aid


def _trend_meta():
    return [{"name": "月份", "role": "time"}, {"name": "净销售额", "role": "metric"}]


def _term_link():
    return LinkResult(matched_elements=[
        {"element_type": "term", "element_id": "term_net_sales", "mention": "销售额"},
        {"element_type": "term", "element_id": "term_gross_sales", "mention": "销售额"},
    ])


def test_chart_preference_applies_and_isolated(conn):
    memory.remember_preference("u1", "chart_preference", "trend", {"chart_type": "line"}, conn=conn)
    base = {"type": "bar", "reason": "rule", "available_types": ["bar", "line", "table"]}
    chart, notes = memory.personalize_chart("u1", base, _trend_meta(), {}, conn=conn)
    assert chart["type"] == "line" and chart["family"] == "trend" and notes
    other, other_notes = memory.personalize_chart("u2", base, _trend_meta(), {}, conn=conn)
    assert other["type"] == "bar" and other_notes == []
    current = memory.list_memories("u1", conn=conn)[0]
    assert current["access_count"] == 1 and current["last_accessed_at"]


def test_chart_preference_respects_family_and_available_types(conn):
    memory.remember_preference("u1", "chart_preference", "trend", {"chart_type": "line"}, conn=conn)
    category = [{"name": "大区", "role": "dimension"}, {"name": "净销售额", "role": "metric"}]
    chart, notes = memory.personalize_chart(
        "u1", {"type": "bar", "available_types": ["bar", "pie", "table"]}, category, {}, conn=conn)
    assert chart["type"] == "bar" and notes == []
    chart, notes = memory.personalize_chart(
        "u1", {"type": "bar", "available_types": ["bar", "table"]}, _trend_meta(), {}, conn=conn)
    assert chart["type"] == "bar" and notes == []


def test_requested_chart_is_one_off_and_negative_is_ignored(conn):
    chart, notes = memory.personalize_chart(
        "u1", {"type": "bar", "available_types": ["bar", "line"]}, _trend_meta(), {},
        "请用折线图展示", conn=conn)
    assert chart["type"] == "line" and "未写入长期偏好" in notes[0]
    assert memory.list_memories("u1", conn=conn) == []
    for question in ("不用折线图", "不要用折线图展示", "别再用折线图了"):
        chart, _ = memory.personalize_chart(
            "u1", {"type": "bar", "available_types": ["bar", "line"]}, _trend_meta(), {},
            question, conn=conn)
        assert chart["type"] == "bar"
    assert memory.requested_chart_type("这个表格里的数据是什么意思") is None
    assert memory.requested_chart_type("请输出为表格") == "table"
    percent_time = [{"name": "月份", "role": "time"}, {"name": "毛利率", "role": "metric"}]
    assert memory.chart_family(percent_time) == "trend"


def test_switch_answer_chart_reuses_snapshot_and_checks_boundaries(conn):
    _answer(conn)
    before = conn.execute("SELECT COUNT(*) AS n FROM query_log").fetchone()["n"]
    switched = memory.switch_answer_chart("u1", "a1", "bar", "s1", source="explicit", conn=conn)
    result = switched["result"]
    assert result["chart"]["type"] == "bar" and result["rows"][0][1] == 100
    assert result["truncated"] is True and result["row_count"] == 7
    assert result["memory_trace"] and "柱状图" in result["memory_trace"][0]
    assert conn.execute("SELECT COUNT(*) AS n FROM query_log").fetchone()["n"] == before
    assert conn.execute("SELECT chart_type FROM answer WHERE id='a1'").fetchone()[0] == "bar"
    with pytest.raises(PermissionError):
        memory.switch_answer_chart("u2", "a1", "bar", conn=conn)
    with pytest.raises(ValueError, match="不支持"):
        memory.switch_answer_chart("u1", "a1", "pie", conn=conn)
    with pytest.raises(KeyError):
        memory.switch_answer_chart("u1", "missing", "bar", conn=conn)


def test_switch_answer_rejects_broken_snapshot(conn):
    _answer(conn, snapshot=False)
    with pytest.raises(ValueError, match="快照不完整"):
        memory.switch_answer_chart("u1", "a1", "bar", conn=conn)
    assert memory.list_memories("u1", conn=conn) == []


def test_supersede_soft_delete_and_audit_events(conn):
    first = memory.remember_preference(
        "u1", "chart_preference", "trend", {"chart_type": "line"}, conn=conn)
    second = memory.remember_preference(
        "u1", "chart_preference", "trend", {"chart_type": "bar"}, conn=conn)
    assert first["id"] != second["id"]
    history = memory.list_memories("u1", include_history=True, conn=conn)
    assert len(history) == 2
    assert {item["status"] for item in history} == {"active", "superseded"}
    memory.delete_memory("u1", second["id"], conn=conn)
    assert memory.list_memories("u1", conn=conn) == []
    events = conn.execute("SELECT event_type FROM user_memory_event").fetchall()
    assert {row[0] for row in events} >= {"created", "superseded", "deleted"}
    with pytest.raises(ValueError):
        memory.delete_memory("u1", second["id"], conn=conn)
    with pytest.raises(PermissionError):
        memory.delete_memory("u2", first["id"], conn=conn)


def test_decay_stops_application_and_access_does_not_renew(conn):
    item = memory.remember_preference(
        "u1", "chart_preference", "trend", {"chart_type": "line"},
        source="chart_switch", conn=conn)
    old = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat().replace("+00:00", "Z")
    conn.execute("UPDATE user_memory SET last_confirmed_at = ? WHERE id = ?", (old, item["id"]))
    conn.commit()
    row = conn.execute("SELECT * FROM user_memory WHERE id = ?", (item["id"],)).fetchone()
    assert memory.effective_confidence(row) < 0.35
    chart, notes = memory.personalize_chart(
        "u1", {"type": "bar", "available_types": ["bar", "line"]}, _trend_meta(), {}, conn=conn)
    assert chart["type"] == "bar" and notes == []
    assert memory.list_memories("u1", conn=conn)[0]["decayed"] is True
    memory.mark_accessed(item["id"], "u1", conn=conn)
    renewed = conn.execute("SELECT last_confirmed_at FROM user_memory WHERE id = ?",
                           (item["id"],)).fetchone()[0]
    assert renewed == old
    # 用户再次明确指定可恢复同一条记忆
    memory.remember_preference("u1", "chart_preference", "trend", {"chart_type": "line"}, conn=conn)
    assert memory.list_memories("u1", conn=conn)[0]["decayed"] is False


def test_term_choice_promotes_only_across_distinct_sessions(conn, snap):
    for sid in ("s1", "s2", "s3"):
        _session(conn, sid)
    first = memory.observe_term_choice("u1", "销售额", "term_net_sales", "s1", conn=conn)
    duplicate = memory.observe_term_choice("u1", "销售额", "term_net_sales", "s1", conn=conn)
    assert first["sessions"] == 1 and duplicate["sessions"] == 1
    assert duplicate["memory"]["status"] == "candidate"
    second = memory.observe_term_choice("u1", "销售额", "term_net_sales", "s2", conn=conn)
    assert second["sessions"] == 2 and not second["promoted"]
    third = memory.observe_term_choice("u1", "销售额", "term_net_sales", "s3", conn=conn)
    assert third["sessions"] == 3 and third["promoted"]
    assert third["memory"]["status"] == "active"
    preferences = memory.get_term_preferences("u1", snap, conn=conn)
    assert preferences["term:销售额"]["selected_key"] == "term_net_sales"
    assert memory.get_term_preferences("u2", snap, conn=conn) == {}


def test_changed_candidate_choice_starts_new_history(conn):
    _session(conn, "s1")
    _session(conn, "s2")
    memory.observe_term_choice("u1", "销售额", "term_net_sales", "s1", conn=conn)
    changed = memory.observe_term_choice("u1", "销售额", "term_gross_sales", "s2", conn=conn)
    assert changed["sessions"] == 1 and changed["memory"]["status"] == "candidate"
    history = memory.list_memories("u1", include_history=True, conn=conn)
    assert len(history) == 2 and {item["status"] for item in history} == {"candidate", "superseded"}


def test_long_term_clarify_priority_and_stale_candidate(snap):
    amb = {"type": "TERM_CONFLICT", "mention": "销售额",
           "candidates": ["term_net_sales", "term_gross_sales"]}
    long_term = {"term:销售额": {"selected_key": "term_net_sales", "label": "净销售额",
                                  "memory_id": "m1"}}
    link = _term_link()
    remaining, notes = chat_service._apply_clarify_memory(
        [amb], SimpleNamespace(), link, {"clarify_choices": {}}, snap, long_term)
    assert remaining == [] and "长期口径偏好" in notes[0]
    assert [m["element_id"] for m in link.matched_elements] == ["term_net_sales"]
    # 同一会话内的明确选择优先于长期偏好
    link = _term_link()
    ctx = {"clarify_choices": {"term:销售额": "term_gross_sales"}}
    _, notes = chat_service._apply_clarify_memory([amb], SimpleNamespace(), link, ctx, snap, long_term)
    assert "本次会话" in notes[0]
    assert [m["element_id"] for m in link.matched_elements] == ["term_gross_sales"]
    long_term["term:销售额"]["selected_key"] = "term_deleted"
    remaining, notes = chat_service._apply_clarify_memory(
        [amb], SimpleNamespace(), _term_link(), {"clarify_choices": {}}, snap, long_term)
    assert remaining == [amb] and notes == []


def test_context_roundtrip_expiry_corruption_and_owner(conn):
    _session(conn)
    state = {"turns": [{"question": str(i), "intent": "DATA_QUERY"} for i in range(7)],
             "clarify_choices": {"term:销售额": "term_net_sales"},
             "last_answer": {"answer_id": "a1", "family": "trend"},
             "clarify_pending": {"secret": "不序列化"}}
    memory.save_session_context("s1", "u1", state, conn=conn)
    loaded = memory.load_session_context("s1", "u1", conn=conn)
    assert len(loaded["turns"]) == 5 and loaded["turns"][-1]["question"] == "6"
    assert "clarify_pending" not in loaded and loaded["last_answer"]["answer_id"] == "a1"
    assert memory.load_session_context("s1", "u2", conn=conn) is None
    with pytest.raises(PermissionError):
        memory.save_session_context("s1", "u2", {}, conn=conn)
    conn.execute("UPDATE chat_context SET expires_at='2000-01-01T00:00:00Z' WHERE session_id='s1'")
    conn.commit()
    assert memory.load_session_context("s1", "u1", conn=conn) is None
    memory.save_session_context("s1", "u1", {"turns": []}, conn=conn)
    conn.execute("UPDATE chat_context SET state_json='{broken' WHERE session_id='s1'")
    conn.commit()
    assert memory.load_session_context("s1", "u1", conn=conn) is None
    with pytest.raises(KeyError):
        memory.save_session_context("missing", "u1", {}, conn=conn)


def test_chat_context_restored_after_cache_clear(conn, monkeypatch):
    _session(conn)
    original_load = memory.load_session_context
    original_save = memory.save_session_context
    monkeypatch.setattr(chat_service.memory, "load_session_context", lambda sid, uid: None)
    # 首次缓存为空；保存后清空 LRU，再从测试库恢复，模拟服务重启
    monkeypatch.setattr(chat_service.memory, "save_session_context",
                        lambda sid, uid, state: original_save(sid, uid, state, conn=conn))
    chat_service._CTX.clear()
    ctx = chat_service._get_ctx("s1", "u1")
    ctx["turns"].append({"question": "四月销售额", "intent": "DATA_QUERY"})
    ctx["clarify_choices"]["term:销售额"] = "term_net_sales"
    chat_service._persist_ctx("u1", "s1", ctx)
    chat_service._CTX.clear()
    monkeypatch.setattr(chat_service.memory, "load_session_context",
                        lambda sid, uid: original_load(sid, uid, conn=conn))
    restored = chat_service._get_ctx("s1", "u1")
    assert restored["turns"][0]["question"] == "四月销售额"
    assert restored["clarify_choices"]["term:销售额"] == "term_net_sales"
    chat_service._CTX.clear()


def test_chat_context_lru_skips_pending(monkeypatch):
    monkeypatch.setattr(chat_service, "MEMORY_CONTEXT_CACHE_SIZE", 2)
    monkeypatch.setattr(chat_service.memory, "load_session_context", lambda sid, uid: None)
    chat_service._CTX.clear()
    chat_service._get_ctx("s1", "u1")["clarify_pending"] = {"clarify_id": "c1"}
    chat_service._get_ctx("s2", "u1")
    chat_service._get_ctx("s3", "u1")
    assert "s1" in chat_service._CTX and "s3" in chat_service._CTX
    assert len(chat_service._CTX) == 2
    chat_service._CTX.clear()


def test_clarify_rejects_other_user_and_forged_option():
    chat_service._CTX.clear()
    pending = {"clarify_id": "c1", "user_id": "u1", "question": "销售额",
               "intent": SimpleNamespace(), "link_result": _term_link(), "round": 1,
               "type": "TERM_CONFLICT", "mention": "销售额",
               "candidates": ["term_net_sales", "term_gross_sales"]}
    chat_service._CTX["s1"] = {"clarify_pending": pending}
    denied = list(chat_service.clarify({"id": "u2"}, "c1", "term_net_sales"))
    assert denied[0]["data"]["code"] == "FORBIDDEN"
    invalid = list(chat_service.clarify({"id": "u1"}, "c1", "term_forged"))
    assert invalid[0]["data"]["code"] == "CLARIFY_OPTION_INVALID"
    chat_service._CTX.clear()


def test_personal_exemplar_recall_forget_and_isolation(conn):
    _answer(conn)
    saved = memory.remember_exemplar("u1", "a1", conn=conn)
    assert saved["category"] == "query_exemplar"
    picked = memory.pick_personal_exemplars("u1", "各月净销售额", "ds_sales", conn=conn)
    assert len(picked) == 1 and picked[0]["source"] == "user_memory"
    assert memory.pick_personal_exemplars("u2", "各月净销售额", "ds_sales", conn=conn) == []
    merged = pick_exemplars(conn, "各月净销售额", "ds_sales", user_id="u1")
    assert merged[0]["source"] == "user_memory"
    assert memory.forget_exemplar("u1", "a1", conn=conn) is True
    assert memory.pick_personal_exemplars("u1", "各月净销售额", "ds_sales", conn=conn) == []
    assert memory.forget_exemplar("u1", "a1", conn=conn) is False


def test_clear_by_category_and_validation(conn):
    memory.remember_preference("u1", "chart_preference", "trend", {"chart_type": "line"}, conn=conn)
    memory.remember_preference("u1", "term_mapping", "销售额", {"term_id": "term_net_sales"}, conn=conn)
    assert memory.clear_memories("u1", "chart_preference", conn=conn) == 1
    assert len(memory.list_memories("u1", conn=conn)) == 1
    assert memory.clear_memories("u1", conn=conn) == 1
    with pytest.raises(ValueError):
        memory.remember_preference("u1", "query_exemplar", "x", {"semql": {}}, conn=conn)
    with pytest.raises(ValueError):
        memory.remember_preference("u1", "chart_preference", "unknown", {"chart_type": "line"}, conn=conn)
    with pytest.raises(ValueError):
        memory.remember_preference("u1", "term_mapping", "销售额", {}, conn=conn)


def test_memory_command_strict_parsing(snap):
    parse = chat_service._parse_memory_command
    assert parse("以后趋势都用折线图", snap)["type"] == "remember_chart"
    assert parse("以后分类对比都用柱状图", snap)["family"] == "category"
    assert parse("生成折线图", snap)["type"] == "switch_chart"
    assert parse("记住销售额指净销售额", snap)["term_id"] == "term_net_sales"
    assert parse("以后销售额都按毛销售额", snap)["term_id"] == "term_gross_sales"
    assert parse("忘掉销售额口径", snap)["mention"] == "销售额"
    assert parse("忘掉全部记忆", snap)["type"] == "forget_all"
    assert parse("你记得我什么", snap)["type"] == "list"
    assert parse("以后都用折线图", snap)["type"] == "memory_error"
    assert parse("记住销售额指不存在", snap)["type"] == "memory_error"
    assert parse("2024年3月到5月净销售额，用折线图展示", snap) is None
    assert parse("以后每个月的净销售额是多少", snap) is None
    assert parse("今后各月的销量趋势", snap) is None


def test_custom_time_choice_keeps_existing_fallback(monkeypatch):
    chat_service._CTX.clear()
    pending = {"clarify_id": "c-time", "user_id": "u1", "question": "销量",
               "intent": SimpleNamespace(time_range=None), "link_result": _term_link(),
               "round": 1, "type": "TIME_MISSING", "mention": None,
               "candidates": ["last_7d", "last_month", "custom"]}
    chat_service._CTX["s1"] = {"clarify_pending": pending}
    monkeypatch.setattr(chat_service, "_persist_ctx", lambda uid, sid, ctx: None)

    def fake_run(*args, **kwargs):
        yield {"event": "result", "data": {"answer_type": "data"}}

    monkeypatch.setattr(chat_service, "_run_data_query", fake_run)
    events = list(chat_service.clarify({"id": "u1"}, "c-time", "custom"))
    assert events[0]["event"] == "result"
    chat_service._CTX.clear()
