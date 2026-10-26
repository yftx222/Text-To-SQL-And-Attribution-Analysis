"""后台任务状态读取用例。"""

from celery.result import AsyncResult

from dataagent.app.schemas.tasks import TaskStatusResponse
from dataagent.engines.common.tasks.celery_app import celery_app


def get_task_status(task_id: str) -> TaskStatusResponse:
    """查询后台任务状态和结果。"""
    task = AsyncResult(task_id, app=celery_app)
    result = task.result if task.successful() else None
    error = str(task.result) if task.failed() else None
    return TaskStatusResponse(
        task_id=task_id,
        state=task.state,
        ready=task.ready(),
        successful=task.successful() if task.ready() else None,
        result=result,
        error=error,
    )
