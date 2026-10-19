"""全链路回归：q01-q12 真实 DeepSeek，逐题断言并输出 test_data/output/<qid>.json + report.md。

运行：python scripts/run_pipeline_test.py
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from chatbi.db import get_meta_conn  # noqa: E402
from chatbi.services.auth import login  # noqa: E402
from chatbi.services.chat_service import ask  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
QUESTIONS_FILE = ROOT / "test_data" / "input" / "questions.json"
EXPECT_FILE = ROOT / "test_data" / "expected" / "expectations.json"
OUT_DIR = ROOT / "test_data" / "output"


def run_question(user: dict, question: str, session_id: str | None = None) -> dict:
    """走完整 ask 事件流，收集为结构化结果。"""
    events = []
    t0 = time.monotonic()
    for ev in ask(user, session_id, question):
        events.append(ev)
    elapsed = int((time.monotonic() - t0) * 1000)
    out = {"question": question, "elapsed_ms": elapsed,
           "events": [{"event": e["event"], "data": e["data"]} for e in events]}
    for ev in events:
        if ev["event"] == "intent":
            out["intent"] = ev["data"]
            session_id = ev["data"].get("session_id", session_id)
        elif ev["event"] == "clarify":
            out["clarify"] = ev["data"]
        elif ev["event"] == "result":
            out["result"] = ev["data"]
        elif ev["event"] == "error":
            out["error"] = ev["data"]
    out["session_id"] = session_id
    return out


def _get(d: dict | None, path: str, default=None):
    cur = d
    for k in path.split("."):
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def evaluate(qid: str, expect: dict, out: dict) -> list[str]:
    """返回失败原因列表（空 = pass）。"""
    fails: list[str] = []
    exp = expect.get("expect", {})
    intent = out.get("intent") or {}
    result = out.get("result") or {}
    clarify = out.get("clarify")
    semql = result.get("semql") or {}
    sql = result.get("sql") or ""
    report = result.get("report") or {}

    def chk(cond, msg):
        if not cond:
            fails.append(msg)

    if "intent" in exp:
        chk(intent.get("intent") == exp["intent"],
            f"intent 期望 {exp['intent']} 实际 {intent.get('intent')}")
    if "query_mode" in exp:
        chk(semql.get("query_mode") == exp["query_mode"],
            f"query_mode 期望 {exp['query_mode']} 实际 {semql.get('query_mode')}")
    if "metrics" in exp:
        chk(all(m in semql.get("metrics", []) for m in exp["metrics"]),
            f"metrics 期望含 {exp['metrics']} 实际 {semql.get('metrics')}")
    if "dimensions" in exp:
        chk(all(d in semql.get("dimensions", []) for d in exp["dimensions"]),
            f"dimensions 期望含 {exp['dimensions']} 实际 {semql.get('dimensions')}")
    if "time_range" in exp:
        want = f"{exp['time_range'][0]}/{exp['time_range'][1]}"
        chk(_get(semql, "time.range") == want,
            f"time.range 期望 {want} 实际 {_get(semql, 'time.range')}")
    if "grain" in exp:
        chk(_get(semql, "time.grain") == exp["grain"],
            f"grain 期望 {exp['grain']} 实际 {_get(semql, 'time.grain')}")
    if "row_count" in exp:
        rc = result.get("row_count", -1)
        chk(exp["row_count"]["min"] <= rc <= exp["row_count"]["max"],
            f"row_count 期望 {exp['row_count']} 实际 {rc}")
    if "chart_type_in" in exp:
        chk(_get(result, "chart.type") in exp["chart_type_in"],
            f"chart.type 期望 ∈{exp['chart_type_in']} 实际 {_get(result, 'chart.type')}")
    if "sql_contains" in exp:
        for frag in exp["sql_contains"]:
            chk(frag in sql, f"SQL 缺少片段 {frag!r}")
    if exp.get("applied_rules_nonempty"):
        chk(bool(result.get("applied_rules")), "applied_rules 为空")
    if "limit" in exp:
        chk(semql.get("limit") == exp["limit"],
            f"limit 期望 {exp['limit']} 实际 {semql.get('limit')}")
    if "order_by" in exp:
        chk(bool(semql.get("order_by")), "order_by 为空")
    if "clarify" in exp:
        ce = exp["clarify"]
        chk(clarify is not None, "未产生 clarify 事件")
        if clarify:
            chk(clarify.get("type") == ce.get("type"),
                f"clarify.type 期望 {ce.get('type')} 实际 {clarify.get('type')}")
            if "candidates" in ce:
                keys = {o["key"] for o in clarify.get("options", [])}
                chk(all(c in keys for c in ce["candidates"]),
                    f"clarify 选项 {keys} 缺少 {ce['candidates']}")
            chk(clarify.get("round") == ce.get("round", 1), "clarify.round 不符")
    if "is_significant" in exp:
        chk(report.get("is_significant", result.get("is_significant")) == exp["is_significant"],
            f"is_significant 期望 {exp['is_significant']} 实际 {report.get('is_significant', result.get('is_significant'))}")
    if "top_cause" in exp:
        l1 = (report.get("drill_tree") or [{}])[0]
        top1 = (l1.get("top") or [{}])[0]
        chk(l1.get("dimension_id") == exp["top_cause"]["dimension"]
            and top1.get("tech_value") == exp["top_cause"]["value"],
            f"top_cause 期望 {exp['top_cause']} 实际 {l1.get('dimension_id')}/{top1.get('tech_value')}")
    if "level2_cause" in exp:
        l2 = [n for n in report.get("drill_tree", []) if n.get("level") == 2]
        chk(bool(l2), "无第二层下钻")
        if l2:
            top1 = (l2[0].get("top") or [{}])[0]
            chk(l2[0].get("dimension_id") == exp["level2_cause"]["dimension"]
                and top1.get("tech_value") == exp["level2_cause"]["value"],
                f"level2 期望 {exp['level2_cause']} 实际 {l2[0].get('dimension_id')}/{top1.get('tech_value')}")
    if exp.get("events_nonempty"):
        chk(bool(report.get("events")), "events 为空")
    if exp.get("has_baseline"):
        chk(bool(_get(result, "fluctuation.baseline_series")), "fluctuation.baseline_series 为空")
    if "answer_contains_any" in exp:
        text = (result.get("summary") or "") + (result.get("narrative") or "")
        chk(any(w in text for w in exp["answer_contains_any"]),
            f"回答文本不含 {exp['answer_contains_any']} 任一词：{text[:80]}")
    if exp.get("refused"):
        at = result.get("answer_type")
        text = result.get("summary") or ""
        chk(at == "text" and ("只支持数据查询" in text or "不允许" in text)
            or intent.get("intent") in ("UNSUPPORTED",),
            f"未拒绝：answer_type={at} intent={intent.get('intent')}")
    if exp.get("no_write_executed"):
        chk(not sql, "拒绝场景不应产生 SQL")
    if "inherits" in exp:
        chk(bool(intent.get("inherits")) or "HB" in sql or any("HB" in str(r) for r in result.get("rows", [])),
            "未体现上下文继承（intent.inherits=false 且结果无 HB）")
    if "filters_contain" in exp:
        fc = exp["filters_contain"]
        hit = any(f.get("dimension") == fc["dimension"]
                  and all(v in f.get("values", []) for v in fc["values"])
                  for f in semql.get("filters", []))
        chk(hit, f"filters 缺少 {fc}")
    if "rows_only_region" in exp:
        rows = result.get("rows", [])
        cols = result.get("columns", [])
        dim_idx = 0 if cols else -1
        ok = bool(rows) and all(str(r[dim_idx]) in ("HD", exp["rows_only_region"]) for r in rows)
        chk(ok, f"结果行含非{exp['rows_only_region']}数据: {rows[:5]}")
    if "explain_contains" in exp:
        text = "；".join(result.get("match_explain") or [])
        chk(all(w in text for w in exp["explain_contains"]),
            f"match_explain 不含 {exp['explain_contains']}: {text}")
    if "matched_business_term" in exp:
        bt = semql.get("business_terms", [])
        matched_terms = [m.get("element_id") for m in (result.get("matched_elements") or [])
                         if m.get("element_type") == "term"]
        chk(exp["matched_business_term"] in bt or exp["matched_business_term"] in matched_terms,
            f"未命中 business_term：business_terms={bt} matched={matched_terms}")
    return fails


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    questions = json.loads(QUESTIONS_FILE.read_text(encoding="utf-8"))
    expects = json.loads(EXPECT_FILE.read_text(encoding="utf-8"))
    qmap = {q["id"]: q for q in questions}

    conn = get_meta_conn()
    llm_before = conn.execute("SELECT COUNT(*) c FROM llm_call_log").fetchone()["c"]
    conn.close()

    analyst = login("analyst", "Init@123456")["user"]
    east_mgr = login("east_mgr", "Init@123456")["user"]

    results: dict[str, dict] = {}
    t_all = time.monotonic()

    for q in questions:
        qid = q["id"]
        if qid == "q09":
            continue  # q09 是会话追问题，在循环后单独按 q01 的 session 连续跑两轮
        user = east_mgr if q.get("user") == "east_mgr" else analyst
        out = run_question(user, q["question"])
        results[qid] = out
        (OUT_DIR / f"{qid}.json").write_text(
            json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        fails = evaluate(qid, expects[qid], out)
        out["assertion_fails"] = fails
        print(f"[{qid}] {'PASS' if not fails else 'FAIL'}  {q['question']}"
              + ("" if not fails else f"  → {fails}"))

    # q09 第二轮（同 session 追问"那华北呢"）
    sid = results["q01"].get("session_id")
    out9b = run_question(analyst, qmap["q09"]["question"], session_id=sid)
    results["q09"] = {**out9b, "round1_ref": results["q01"]["question"]}
    (OUT_DIR / "q09.json").write_text(
        json.dumps(results["q09"], ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    fails9 = evaluate("q09", expects["q09"], out9b)
    results["q09"]["assertion_fails"] = fails9
    print(f"[q09] {'PASS' if not fails9 else 'FAIL'}  （轮2）{qmap['q09']['question']}"
          + ("" if not fails9 else f"  → {fails9}"))

    total_s = time.monotonic() - t_all
    conn = get_meta_conn()
    llm_calls = conn.execute("SELECT COUNT(*) c FROM llm_call_log").fetchone()["c"] - llm_before
    conn.close()

    # ---- report.md ----
    lines = ["# InsightChat 全链路回归报告", "",
             f"- 总耗时：{total_s:.1f}s；LLM 调用次数：{llm_calls}",
             f"- 结论：{sum(1 for q in results if not results[q]['assertion_fails'])}/{len(results)} 通过", "",
             "| 题 | 问题 | 结果 | 耗时 | 失败原因 |", "|---|---|---|---|---|"]
    for q in questions:
        qid = q["id"]
        out = results[qid]
        fails = out["assertion_fails"]
        lines.append(f"| {qid} | {q['question']} | {'✅ PASS' if not fails else '❌ FAIL'} "
                     f"| {out['elapsed_ms']}ms | {'；'.join(fails) or '-'} |")
    lines.append("")
    for q in questions:
        qid = q["id"]
        out = results[qid]
        lines += [f"\n## {qid} {q['question']}", "",
                  f"- 结果：{'PASS' if not out['assertion_fails'] else 'FAIL'}",
                  f"- intent：{json.dumps(out.get('intent'), ensure_ascii=False)}",]
        if out.get("clarify"):
            lines.append(f"- clarify：{json.dumps(out['clarify'], ensure_ascii=False)}")
        r = out.get("result") or {}
        if r.get("sql"):
            lines += ["- SQL：", "```sql", r["sql"], "```"]
        if r.get("chart"):
            lines.append(f"- 图表：{r['chart'].get('type')}（{r['chart'].get('reason')}）")
        if r.get("summary"):
            lines.append(f"- 摘要：{r['summary']}")
        if r.get("report"):
            rep = r["report"]
            lines.append(f"- 归因：is_significant={rep.get('is_significant')} "
                         f"values={json.dumps(rep.get('values'), ensure_ascii=False)}")
            lines.append(f"- drill_tree：{json.dumps(rep.get('drill_tree'), ensure_ascii=False)}")
            lines.append(f"- events：{json.dumps(rep.get('events'), ensure_ascii=False)}")
            if r.get("narrative"):
                lines.append(f"- narrative：{r['narrative']}")
        if out.get("assertion_fails"):
            lines.append(f"- 失败断言：{'；'.join(out['assertion_fails'])}")
    (OUT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")

    print(f"\n报告已写入 {OUT_DIR / 'report.md'}")
    print(f"总耗时 {total_s:.1f}s，LLM 调用 {llm_calls} 次")


if __name__ == "__main__":
    main()
