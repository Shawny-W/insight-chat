"""全量初始化：建 meta 库 schema → 导种子 → 发布 v1 → Evidence 真实执行 → 建词典 → 打印摘要。

运行：python scripts/init_db.py
幂等：已存在 published 版本时跳过种子导入（Evidence 仍会重跑刷新）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from chatbi.config import BIZ_DB_PATH, DICT_FILE, META_DB_PATH  # noqa: E402
from chatbi.db import get_biz_conn, get_meta_conn  # noqa: E402
from chatbi.semantic.dictionary import build_dictionary  # noqa: E402
from chatbi.semantic.schema import init_schema  # noqa: E402
from chatbi.semantic.seed import run_seed  # noqa: E402
from chatbi.semantic.store import load_snapshot  # noqa: E402


def main() -> None:
    if not BIZ_DB_PATH.exists():
        print(f"业务库不存在: {BIZ_DB_PATH}\n请先运行: python scripts/gen_retail_data.py")
        sys.exit(1)

    print(f"[1/4] 建立元数据库 schema: {META_DB_PATH}")
    conn = get_meta_conn()
    init_schema(conn)
    print("  schema 就绪（全部表 CREATE IF NOT EXISTS）")

    print("[2/4] 导入种子并发布语义层 v1 ...")
    biz = get_biz_conn(read_only=True)
    result = run_seed(conn, biz)
    if result["already_published"]:
        print(f"  已存在 published 版本 v{result['version']}，种子导入跳过（幂等）")
    else:
        print(f"  语义层 v{result['version']} 已发布（published）")
    print("  语义层对象计数:")
    for table, n in result["semantic_counts"].items():
        print(f"    {table:<22} {n}")
    print("  基础数据计数:")
    for table, n in result["foundation"].items():
        print(f"    {table:<22} {n}")

    print("[3/4] 对零售库真实执行 Evidence SQL ...")
    for ev in result["evidences"]:
        mark = "✗" if ev["error"] else "✓"
        print(f"  {mark} {ev['id']:<26} {ev['result']}")
    if any(ev["error"] for ev in result["evidences"]):
        print("存在执行失败的 Evidence，请检查业务库")
        sys.exit(1)

    print("[4/4] 重建 jieba 自定义词典 ...")
    snap = load_snapshot(conn)
    n = build_dictionary(snap, conn)
    print(f"  词典已写入 {DICT_FILE}（{n} 个词条）")

    biz.close()
    conn.close()

    print("\n===== 初始化摘要 =====")
    print(snap.summary())
    print("初始化完成。")


if __name__ == "__main__":
    main()
