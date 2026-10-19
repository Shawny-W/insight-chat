"""归因引擎自验：直接调 attribute() 跑"2024-04 vs 2024-05 销量"归因（不走 LLM 意图），
打印完整归因 JSON（waterfall/drill_tree/events）+ narrative（真实 DeepSeek heavy 模型）。

运行：python scripts/dev_insight.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from chatbi.insight.attribution import attribute  # noqa: E402
from chatbi.insight.narrative import narrate  # noqa: E402
from chatbi.semantic.store import load_snapshot  # noqa: E402


def main() -> None:
    snap = load_snapshot()
    result = attribute(
        "metric_qty", "2024-04", "2024-05",
        [{"dimension": "dim_channel", "op": "not_in", "values": ["TEST"]}],
        snap, [])
    report = result.to_dict()

    print("===== 归因 JSON（2024-04 vs 2024-05，指标=销量 metric_qty） =====")
    print(json.dumps(report, ensure_ascii=False, indent=2))

    print("\n===== narrative（DeepSeek heavy 生成；失败则模板降级） =====")
    text, degraded = narrate(report)
    print(f"[degraded={degraded}]")
    print(text)

    # 硬性断言（任务书验收口径）
    assert report["is_significant"] is True
    l1 = report["drill_tree"][0]
    assert l1["dimension_id"] == "dim_region"
    assert l1["top"][0]["tech_value"] == "HD"
    l2 = [n for n in report["drill_tree"] if n["level"] == 2]
    assert l2 and l2[0]["path"] == ["华东"] and l2[0]["top"][0]["tech_value"] == "A"
    event_names = {e["name"] for e in report["events"]}
    assert "华东物流线路检修" in event_names and "五一促销主会场活动" in event_names
    assert text.strip()
    print("\n归因断言全部通过 ✓")


if __name__ == "__main__":
    main()
