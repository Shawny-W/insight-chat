"""FastAPI 入口：create_app() 挂载路由 + 静态目录 + 全局异常→统一错误包 + CORS。"""

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from chatbi.api import routes_admin, routes_auth, routes_chat, routes_feedback, \
    routes_history, routes_insight
from chatbi.api.deps import ApiError, new_trace_id

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def create_app() -> FastAPI:
    app = FastAPI(title="InsightChat", version="0.1.0",
                  description="企业级对话式 BI（NL2SQL + 洞察归因）本地演示版")

    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"])

    # 统一错误包
    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError):
        return JSONResponse(status_code=exc.http_status,
                            content={"code": exc.code, "message": exc.message,
                                     "trace_id": new_trace_id()})

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception):
        logger.exception("未捕获异常: %s", exc)
        return JSONResponse(status_code=500,
                            content={"code": 5000,
                                     "message": "系统开小差了，请稍后再试",
                                     "trace_id": new_trace_id()})

    @app.on_event("startup")
    def _startup() -> None:
        # 启动时热加载 jieba 自定义词典（词典是发布的派生物）
        from chatbi.semantic.dictionary import load_dictionary
        load_dictionary()

    prefix = "/api/v1"
    app.include_router(routes_auth.router, prefix=prefix)
    app.include_router(routes_chat.router, prefix=prefix)
    app.include_router(routes_insight.router, prefix=prefix)
    app.include_router(routes_history.router, prefix=prefix)
    app.include_router(routes_feedback.router, prefix=prefix)
    app.include_router(routes_admin.router, prefix=prefix)

    # 静态页（演示前端）：/ → index.html
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
    return app


app = create_app()
