"""Assistant 后台任务的消息发布入口。"""

from uuid import UUID

from loguru import logger

from dataagent.engines.common.tasks.celery_app import celery_app
from dataagent.engines.common.tasks.submission import TaskSubmission


def enqueue_conversation_title(
    user_id: int,
    conversation_id: UUID,
    expected_title: str,
    user_text: str,
) -> TaskSubmission:
    """提交会话标题生成任务。"""
    return _submit(
        "dataagent.assistant.generate_conversation_title",
        [user_id, str(conversation_id), expected_title, user_text],
    )


def enqueue_conversation_deletion(
    user_id: int,
    conversation_id: UUID,
) -> TaskSubmission:
    """提交会话物理资源删除任务。"""
    return _submit(
        "dataagent.assistant.delete_conversation_resources",
        [user_id, str(conversation_id)],
    )


def _submit(name: str, args: list[object]) -> TaskSubmission:
    """按中央路由配置提交助手后台任务。"""
    '''
    将这些信息发送给 Redis，返回任务 ID，然后由 Celery Worker 取出任务并执行
    标题生成任务进入lightweight 队列
    后端 API 和 Worker 是两个独立进程。API 负责接收前端请求，Worker 负责执行后台工作。
    API 将任务名称和参数放进 Redis，Worker 从 Redis 取出来，再调用自己代码中注册的函数。
    Redis 负责保存、交接任务，执行者是 Worker。
    '''
    task = celery_app.send_task(
        name,
        args=args,
    )
    submission = TaskSubmission(task_id=task.id)
    logger.info(f"助手后台任务已提交: task_id={submission.task_id}, name={name}")
    return submission
