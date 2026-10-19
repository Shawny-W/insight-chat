"""端到端真实 LLM 自验：
- q01「2024年4月各大区的净销售额」: intent → rewrite → linking → generate → compile → gateway → execute
- q04「四月的销售额怎么样」: intent → linking → 歧义复核 → 澄清卡片（不生成 SQL）

运行：python scripts/dev_e2e_llm.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from chatbi.nlp.clarify import build_clarify, detect_ambiguities  # noqa: E402
from chatbi.nlp.intent import classify  # noqa: E402
from chatbi.nlp.linking import link  # noqa: E402
from chatbi.nlp.rewrite import rewrite  # noqa: E402
from chatbi.semantic.store import load_snapshot  # noqa: E402
from chatbi.services.query_service import run_query  # noqa: E402


def banner(t: str) -> None:
    print("\n" + "=" * 72)
    print(t)
    print("=" * 72)


def run_q01(snap) -> None:
    q = "2024年4月各大区的净销售额"
    banner(f"q01 端到端: {q}")

    intent = classify(q, [], snap)
    print(f"\n[意图] intent={intent.intent.value} confidence={intent.confidence} "
          f"degraded={intent.degraded}")
    print(f"  mentions={intent.mentions}")
    print(f"  time_range={intent.time_range} grain={intent.grain} dataset={intent.dataset_id}")

    rewritten, inherits, topic_changed = rewrite(q, [])
    print(f"\n[改写] rewritten={rewritten}（inherits={inherits}, topic_changed={topic_changed}）")

    lr = link(rewritten, intent.mentions, snap)
    print("\n[linking] matched_elements:")
    for m in lr.matched_elements:
        print(f"  - {m}")
    if lr.unmatched:
        print(f"  unmatched={lr.unmatched}")

    ambs = detect_ambiguities(rewritten, intent, lr, snap)
    print(f"\n[歧义复核] {ambs or '无歧义，继续查询链路'}")

    result = run_query(rewritten, user_id="usr_analyst", mentions=intent.mentions,
                       time_range=intent.time_range, grain=intent.grain, snapshot=snap)
    print("\n[SemQL]")
    print(json.dumps(result["semql"], ensure_ascii=False, indent=2))
    print("\n[SQL（网关后）]")
    print(result["sql"])
    print("\n[applied_rules]")
    for a in result["applied_rules"]:
        print(f"  - {a}")
    print(f"\n[结果] {result['row_count']} 行, columns={result['columns']}")
    for row in result["rows"]:
        print("   ", row)

    # 硬性断言（任务书验收口径）
    assert result["row_count"] == 4, f"q01 应返回 4 行，实际 {result['row_count']}"
    assert "completed" in result["sql"], "SQL 应含 status='completed' 口径"
    assert "TEST" in result["sql"], "SQL 应含测试渠道排除"
    print("\nq01 断言通过：4 行；SQL 含 completed 口径与 TEST 渠道排除 ✓")


def run_q04(snap) -> None:
    q = "四月的销售额怎么样"
    banner(f"q04 澄清链路: {q}")

    intent = classify(q, [], snap)
    print(f"[意图] intent={intent.intent.value} mentions={intent.mentions} "
          f"time_range={intent.time_range}")

    lr = link(q, intent.mentions, snap)
    print("[linking] matched:")
    for m in lr.matched_elements:
        print(f"  - {m}")

    ambs = detect_ambiguities(q, intent, lr, snap)
    print("\n[歧义复核 ambiguities]")
    print(json.dumps(ambs, ensure_ascii=False, indent=2))

    card = build_clarify(ambs, snap, round=1)
    assert card is not None, "q04 应产出澄清卡片"
    assert card.type == "TERM_CONFLICT", f"q04 应为 TERM_CONFLICT，实际 {card.type}"
    print("\n[澄清卡片]")
    print(json.dumps(card.to_dict(), ensure_ascii=False, indent=2))
    print("\nq04 断言通过：TERM_CONFLICT 澄清卡片已生成，未生成任何 SQL ✓")


def main() -> None:
    snap = load_snapshot()
    run_q01(snap)
    run_q04(snap)
    print("\n全部端到端自验通过。")


if __name__ == "__main__":
    main()
