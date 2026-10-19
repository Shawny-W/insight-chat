"""SQL 执行器：biz 库只读执行，wall-clock 超时中断 + 行数上限。

契约：execute(sql, timeout=30, max_rows=10000) -> ResultSet(columns, rows, row_count, truncated)
"""

import sqlite3
import time
from dataclasses import dataclass, field

from chatbi.db import get_biz_conn


class QueryTimeoutError(Exception):
    """执行超时（progress handler 中断）。"""


@dataclass
class ResultSet:
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False


def execute(sql: str, timeout: int = 30, max_rows: int = 10000,
            conn: sqlite3.Connection | None = None) -> ResultSet:
    """执行 SQL 并返回受限结果集；超时抛 QueryTimeoutError。"""
    own_conn = conn is None
    if own_conn:
        conn = get_biz_conn(read_only=True)
    start = time.monotonic()

    def _progress() -> int:
        return 1 if time.monotonic() - start > timeout else 0

    try:
        conn.set_progress_handler(_progress, 1000)
        cur = conn.execute(sql)
        columns = [d[0] for d in cur.description or []]
        rows = cur.fetchmany(max_rows + 1)
        truncated = len(rows) > max_rows
        if truncated:
            rows = rows[:max_rows]
        return ResultSet(columns=columns, rows=[tuple(r) for r in rows],
                         row_count=len(rows), truncated=truncated)
    except sqlite3.OperationalError as exc:
        if "interrupted" in str(exc).lower():
            raise QueryTimeoutError(f"查询超时（>{timeout}s），被中断") from exc
        raise
    finally:
        conn.set_progress_handler(None, 0)
        if own_conn:
            conn.close()
