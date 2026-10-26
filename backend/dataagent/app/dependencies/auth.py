"""登录认证接口的请求依赖。"""

from typing import Annotated

from fastapi import Depends, Request

from dataagent.app.dependencies import WebResourcesDep
from dataagent.app.dependencies.identity import IdentitySessionDep
from dataagent.app.services.identity.auth import AuthService
from dataagent.engines.contract.config import cfg
from dataagent.engines.identity.auth.rate_limit import AuthRateLimitService
from dataagent.engines.identity.repositories.identity import IdentityPGRepo


def _get_auth_service(session: IdentitySessionDep) -> AuthService:
    """使用当前请求会话创建认证服务。"""
    return AuthService(IdentityPGRepo(session), cfg.auth)


AuthServiceDep = Annotated[AuthService, Depends(_get_auth_service)]


def _get_auth_rate_limit_service(resources: WebResourcesDep) -> AuthRateLimitService:
    """读取应用持有的认证限流服务。"""
    return resources.auth_rate_limit


AuthRateLimitServiceDep = Annotated[
    AuthRateLimitService, Depends(_get_auth_rate_limit_service)
]


def get_client_ip(request: Request) -> str:
    """读取 ASGI 连接提供的客户端地址。"""
    return request.client.host if request.client is not None else "unknown"
