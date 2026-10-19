"""SQLite 连接工具：元数据库（meta）与业务库（biz）分离。

- get_meta_conn()：元数据库（语义层/用户/权限/历史等），写操作频繁
- get_biz_conn()：业务演示库 retail.db，原则上只读
两者均开启外键约束、Row 工厂。
"""

import sqlite3

from chatbi.config import META_DB_PATH, BIZ_DB_PATH, ensure_dirs


def _connect(path, *, read_only: bool = False) -> sqlite3.Connection:
    ensure_dirs()
    if read_only:
        # URI 只读模式，防止任何意外写入业务库
        uri = f"file:{path.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    else:
        conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_meta_conn() -> sqlite3.Connection:
    """元数据库连接（读写）。"""
    return _connect(META_DB_PATH)


def get_biz_conn(read_only: bool = True) -> sqlite3.Connection:
    """业务库连接（默认只读；生成演示数据时用 read_only=False）。"""
    return _connect(BIZ_DB_PATH, read_only=read_only)
