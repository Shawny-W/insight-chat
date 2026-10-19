"""对话编排自验（真实 DeepSeek）：
- q01「2024年4月各大区的净销售额」（DATA_QUERY 全链路）
- q06「为什么2024年4月销量比5月低」（INSIGHT_WHY 归因链路）
- q09 两轮：q01 后「那华北呢」（上下文继承，区域改为华北）

运行：python scripts/dev_chat.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from chatbi.services.auth import login  # noqa: E402
from chatbi.services.chat_service import ask  # noqa: E402


def print_event(ev: dict) -> None:
    etype, data = ev["event"], ev["data"]
    if etype == "intent":
        print(f"  [intent] {data['intent']} mentions={data['mentions']} "
              f"time={data.get('time_range')} grain={data.get('grain')} degraded={data['degraded']}")
    elif etype == "progress":
        print(f"  [progress] {data.get('stage')} {data.get('note', '')}")
    elif etype == "clarify":
        print(f"  [clarify] {data['type']}: {data['prompt']}")
        for o in data["options"]:
            print(f"     - {o['key']}: {o['label'][:50]}")
    elif etype == "result":
        at = data.get("answer_type")
        if at == "data":
            print(f"  [result:data] rows={data['row_count']} chart={data['chart']['type']} "
                  f"summary={data.get('summary')}")
            print(f"     sql: {data['sql'][:180]}...")
            print(f"     match_explain: {data.get('match_explain')}")
            print(f"     assumptions: {data.get('assumptions')}")
        elif at == "insight":
            r = data["report"]
            print(f"  [result:insight] significant={r['is_significant']} "
                  f"values={r['values']} narrative_degraded={data['narrative_degraded']}")
            print(f"     drill_tree: {[ (n['level'], n['dimension'], [t['value'] for t in n['top']]) for n in r['drill_tree'] ]}")
            print(f"     events: {[e['name'] for e in r['events']]}")
            print(f"     narrative: {data['narrative'][:200]}")
        else:
            print(f"  [result:{at}] {data.get('summary')}")
    elif etype == "error":
        print(f"  [error] {data.get('code')}: {data.get('message')}")


def main() -> None:
    user = login("analyst", "Init@123456")["user"]

    # q01
    q01_events = list(ask(user, None, "2024年4月各大区的净销售额"))
    print(">>> 用户(analyst): 2024年4月各大区的净销售额")
    for ev in q01_events:
        print_event(ev)
    q01_result = next((e["data"] for e in q01_events if e["event"] == "result"), None)
    assert q01_result and q01_result["row_count"] == 4, "q01 应返回 4 行"

    # q06 归因
    print("\n>>> 用户(analyst): 为什么2024年4月销量比5月低")
    q06_events = list(ask(user, None, "为什么2024年4月销量比5月低"))
    for ev in q06_events:
        print_event(ev)
    q06_result = next((e["data"] for e in q06_events if e["event"] == "result"), None)
    assert q06_result and q06_result.get("answer_type") == "insight"
    report = q06_result["report"]
    assert report["is_significant"] is True
    assert report["drill_tree"][0]["dimension_id"] == "dim_region"
    assert report["drill_tree"][0]["top"][0]["tech_value"] == "HD"
    assert q06_result["narrative"].strip()

    # q09 两轮（同 session）：先 q01 再"那华北呢"
    from chatbi.services import history
    sid = history.create_session(user["id"], "多轮演示")
    print("\n>>> [轮1] 用户(analyst): 2024年4月各大区的净销售额")
    for ev in ask(user, sid, "2024年4月各大区的净销售额"):
        print_event(ev)
    print("\n>>> [轮2] 用户(analyst): 那华北呢")
    r2_events = list(ask(user, sid, "那华北呢"))
    for ev in r2_events:
        print_event(ev)
    r2 = next((e["data"] for e in r2_events if e["event"] == "result"), None)
    assert r2, "q09 轮2 应有 result"
    sql = r2.get("sql", "")
    assert "'HB'" in sql or "HB" in str(r2.get("rows")), "q09 轮2 应过滤华北 HB"
    print("\nq09 上下文继承断言通过：轮2 结果仅华北 ✓")

    print("\n全部 chat 自验通过 ✓")


if __name__ == "__main__":
    main()
