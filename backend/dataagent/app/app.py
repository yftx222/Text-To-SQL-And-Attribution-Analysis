from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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
    app.include_router(
        attachment_router,
        prefix="/api/v1/chat/attachment",
    )
    app.include_router(meta_router, prefix="/api/v1/meta")
    app.include_router(task_router, prefix="/api/v1/tasks")


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
    setup_logger()
    app = FastAPI(lifespan=lifespan, responses=_ERROR_RESPONSES)
    _register_middlewares(app)
    register_exception_handlers(app)
    _register_routes(app)
    return app


app = _create_app()
