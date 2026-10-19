"""InsightChat 服务启动入口：uvicorn 127.0.0.1:8000。

运行：python scripts/run_server.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn  # noqa: E402

if __name__ == "__main__":
    uvicorn.run("chatbi.api.main:app", host="127.0.0.1", port=8000, log_level="info")
