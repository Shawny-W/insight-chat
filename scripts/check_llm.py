"""DeepSeek 连通性自检：真实调用一次 light 模型（问"1+1=?"）。

运行：python scripts/check_llm.py
同时验证 llm_call_log 落库（先确保 meta 库 schema 存在）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from chatbi.db import get_meta_conn  # noqa: E402
from chatbi.llm import chat_text  # noqa: E402
from chatbi.semantic.schema import init_schema  # noqa: E402


def main() -> None:
    # 先确保 meta 库存在 llm_call_log（连通性自检可能先于 init_db 运行）
    conn = get_meta_conn()
    init_schema(conn)

    answer = chat_text(
        task="connectivity_check",
        system="你是计算器，只回答数字结果，不要输出任何其他内容。",
        user="1+1=?",
        heavy=False,
    )
    print(f"DeepSeek 连通性自检通过（light 模型），问题: 1+1=? → 回答: {answer}")

    row = conn.execute(
        "SELECT task, model, latency_ms, error FROM llm_call_log "
        "WHERE task='connectivity_check' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row:
        print(f"llm_call_log 已落库: model={row['model']}, latency={row['latency_ms']}ms, error={row['error']}")
    else:
        print("警告: llm_call_log 未落库")
        sys.exit(1)
    conn.close()


if __name__ == "__main__":
    main()
