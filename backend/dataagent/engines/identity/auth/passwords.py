"""Argon2id 密码哈希及进程内并发控制。"""

import asyncio
from functools import lru_cache

from anyio import to_thread
from pwdlib import PasswordHash

ARGON2_MAX_CONCURRENCY = 2


class Argon2PasswordManager:
    """基于 Argon2id 的异步密码哈希实现。"""

    def __init__(self, *, max_concurrency: int = ARGON2_MAX_CONCURRENCY) -> None:
        """初始化 Argon2id 哈希器和并发限制。"""
        if max_concurrency <= 0:
            raise ValueError("max_concurrency 必须为正整数")
        #创建 argon2 的推荐配置哈希器（argon2-cffi 库）
        self._password_hash = PasswordHash.recommended()
        # _dummy_hash：生成一个假密码哈希，用于防止时序攻击（timing attack）。当用户不存在时，
        # 也跑一次 verify 消耗相同时间，避免攻击者通过响应时间差判断用户名是否存在。
        self._dummy_hash = self._password_hash.hash("dataagent-dummy-password")
        # asyncio.Semaphore：用于限制并发访问的信号量，并发太高会拖垮线程池和 CPU。
        # 确保同时只有 max_concurrency 个协程可以执行哈希计算。
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def hash(self, password: str) -> str:
        """在线程池计算密码哈希。"""
        async with self._semaphore:
            return await to_thread.run_sync(self._password_hash.hash, password)

    async def verify(self, password: str, password_hash: str) -> bool:
        """在线程池校验密码。"""
        async with self._semaphore:
            return await to_thread.run_sync(
                self._password_hash.verify,
                password,
                password_hash,
            )

    async def verify_dummy_password(self, password: str) -> None:
        """为未知账号执行等价密码校验，避免暴露账号是否存在。"""
        await self.verify(password, self._dummy_hash)


@lru_cache(maxsize=1)
def get_password_manager() -> Argon2PasswordManager:
    """复用进程内密码哈希器和并发配额。"""
    return Argon2PasswordManager()
