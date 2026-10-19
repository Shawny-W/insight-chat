"""开发自验（不走 LLM）：手工构造 3 个 SemQL 跑 compile → gateway → execute。

覆盖：① 大区净销售额分组；② 按月净销售额 + 环比（mom）；③ 华东订单明细（detail）。
另演示 east_mgr 行策略（仅华东数据）。
运行：python scripts/dev_query.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from chatbi.db import get_meta_conn  # noqa: E402
from chatbi.query.compiler import compile as compile_semql  # noqa: E402
from chatbi.query.executor import execute  # noqa: E402
from chatbi.query.gateway import check_and_rewrite_detail  # noqa: E402
from chatbi.query.semql import SemQL  # noqa: E402
from chatbi.semantic.store import load_snapshot  # noqa: E402
from chatbi.services.query_service import load_policies  # noqa: E402


def show(title: str, semql: SemQL, snap, policies) -> None:
    print("=" * 70)
    print(f"【{title}】")
    print("SemQL:", semql.model_dump_json(exclude_none=True))
    compiled = compile_semql(semql, snap)
    final_sql, notes = check_and_rewrite_detail(compiled.sql, policies, snap)
    print("\n-- 最终 SQL（网关后）--")
    print(final_sql)
    print("\n已应用规则/策略:")
    for a in compiled.applied_rules + notes:
        print(f"  - {a}")
    rs = execute(final_sql)
    print(f"\n结果 {rs.row_count} 行（截断={rs.truncated}）: {rs.columns}")
    for row in rs.rows[:12]:
        print("   ", row)
    print()


def main() -> None:
    snap = load_snapshot()
    conn = get_meta_conn()
    analyst_policies = load_policies(conn, "usr_analyst")      # 仅 dataset 级，无行策略
    east_policies = load_policies(conn, "usr_east_mgr")        # 行策略: dim_region IN [HD]
    conn.close()

    # ① 2024年4月各大区净销售额（analyst 全量）
    show("① 2024年4月各大区净销售额（analyst）", SemQL(
        dataset="ds_sales", metrics=["metric_net_sales"], dimensions=["dim_region"],
        time={"field": "orders.order_date", "range": "2024-04-01/2024-04-30", "grain": None},
        order_by=[{"metric": "metric_net_sales", "direction": "desc"}],
        limit=100, query_mode="aggregate",
    ), snap, analyst_policies)

    # ② 2024年3~5月每月净销售额 + 环比
    show("② 2024年3~5月每月净销售额（环比 mom）", SemQL(
        dataset="ds_sales", metrics=["metric_net_sales"], dimensions=[],
        time={"field": "orders.order_date", "range": "2024-03-01/2024-05-31",
              "grain": "month"},
        compare={"type": "mom"}, limit=100, query_mode="aggregate",
    ), snap, analyst_policies)

    # ③ 2024年4月华东订单明细 10 条（detail）
    show("③ 2024年4月华东订单明细 10 条（detail）", SemQL(
        dataset="ds_sales", metrics=[],
        dimensions=["col_orders_order_id", "col_orders_order_date",
                    "col_stores_store_name", "col_orders_channel", "col_orders_status"],
        time={"field": "orders.order_date", "range": "2024-04-01/2024-04-30", "grain": None},
        filters=[{"dimension": "dim_region", "op": "in", "values": ["华东区"]}],
        limit=10, query_mode="detail",
    ), snap, analyst_policies)

    # ④ 同①但 east_mgr（行策略 dim_region IN [HD]）
    show("④ 同①，但用户为 east_mgr（行策略：仅华东）", SemQL(
        dataset="ds_sales", metrics=["metric_net_sales"], dimensions=["dim_region"],
        time={"field": "orders.order_date", "range": "2024-04-01/2024-04-30", "grain": None},
        order_by=[{"metric": "metric_net_sales", "direction": "desc"}],
        limit=100, query_mode="aggregate",
    ), snap, east_policies)


if __name__ == "__main__":
    main()
