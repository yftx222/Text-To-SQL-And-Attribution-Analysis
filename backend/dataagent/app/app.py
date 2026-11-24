from hmac import compare_digest
import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse

from dataagent.app.exceptions.exception_handlers import register_exception_handlers
from dataagent.app.middleware import trace
from dataagent.app.routers.attachments import router as attachment_router
from dataagent.app.routers.auth import router as auth_router
from dataagent.app.routers.chat import router as chat_router
from dataagent.app.routers.identity import router as admin_router
from dataagent.app.routers.metadata import router as meta_router
from dataagent.app.routers.query import router as query_experience_admin_router
from dataagent.app.routers.tasks import task_router
from dataagent.app.schemas.problem import ProblemDetails
from dataagent.app.services.lifecycle import lifespan
from dataagent.engines.common.observability import run_metrics
from dataagent.engines.common.observability.log import setup_logger
from dataagent.engines.contract.config import cfg

_PROBLEM_RESPONSE = {
    "model": ProblemDetails,
    "content": {
        "application/problem+json": {
            "schema": {"$ref": "#/components/schemas/ProblemDetails"}
        }
    },
}
_ERROR_RESPONSES = {
    422: {
        **_PROBLEM_RESPONSE,
        "description": "参数校验失败",
    },
    "default": {
        **_PROBLEM_RESPONSE,
        "description": "Problem Details 错误响应",
    },
}


def _register_routes(app: FastAPI) -> None:
    """注册接口。"""
    app.include_router(auth_router, prefix="/api/v1/auth")
    app.include_router(admin_router, prefix="/api/v1/admin")
    app.include_router(
        query_experience_admin_router,
        prefix="/api/v1/admin/query-experiences",
    )
    app.include_router(chat_router, prefix="/api/v1/chat")
    if os.getenv("QUALITY_PROJECT_ID"):
        from dataagent.app.routers.quality import mount
        mount(app)
    app.include_router(
        attachment_router,
        prefix="/api/v1/chat/attachment",
    )
    app.include_router(meta_router, prefix="/api/v1/meta")
    app.include_router(task_router, prefix="/api/v1/tasks")
    app.add_api_route("/_observability/analysis", _analysis_metrics, methods=["GET"], include_in_schema=False)


async def _analysis_metrics(request: Request) -> PlainTextResponse:
    token = os.getenv("AIOPS_HTTP_METRICS_TOKEN")
    if not token:
        return PlainTextResponse("", status_code=404)
    supplied = request.headers.get("Authorization", "").encode()
    if not compare_digest(supplied, f"Bearer {token}".encode()):
        return PlainTextResponse("", status_code=401)
    return PlainTextResponse(run_metrics.render(), media_type="text/plain; version=0.0.4")


def _register_middlewares(app: FastAPI) -> None:
    """注册中间件。"""
    app.middleware("http")(trace.middleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


def _create_app() -> FastAPI:
    """创建并组装 FastAPI 应用。"""
    if os.getenv("AIOPS_QUALITY_PROFILE"):
        from aiops_quality_bridge import load_profile
        load_profile()
    setup_logger()
    app = FastAPI(lifespan=lifespan, responses=_ERROR_RESPONSES)
    _register_middlewares(app)
    register_exception_handlers(app)
    _register_routes(app)
    return app


app = _create_app()
