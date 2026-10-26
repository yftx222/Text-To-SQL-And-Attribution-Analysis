"""后台任务查询接口。"""

from fastapi import APIRouter

from dataagent.app.dependencies import AdminUserDep
from dataagent.app.schemas.tasks import TaskStatusResponse
from dataagent.app.services.tasks import get_task_status as read_task_status

task_router = APIRouter(tags=["tasks"])


@task_router.get("/{task_id}")
async def get_task_status(
    task_id: str,
    _: AdminUserDep,
) -> TaskStatusResponse:
    """查询后台任务状态和结果。"""
    return read_task_status(task_id)
