"""从运行时取得身份。工具参数中仅暴露业务请求与已有执行编号。"""

from uuid import UUID

from langchain.tools import tool
from sqlalchemy.exc import SQLAlchemyError

from dataagent.engines.agent.common.tools.errors import tool_error
from dataagent.engines.query.contracts import SemanticMonthRequest, SemanticQueryRequest
from dataagent.engines.query.errors import SemanticAuditError


def ontology_tools(service, key, role):
    @tool
    async def recall_ontology():
        """读取本分析可用的售后规则、来源字段与固定版本。"""
        try:
            return {"status": "success", "ontology": await service.bundle(key)}
        except (ValueError, PermissionError, SQLAlchemyError) as exc:
            return tool_error("没有可用的售后业务规则", exc)

    @tool
    async def execute_semantic_query(
        request: SemanticMonthRequest | SemanticQueryRequest,
    ):
        """查询修复后30天内发生的同类复发。month 限定后事件发生月份；指定客户、设备分别填 customer_ids、device_ids。"""
        try:
            return {
                "status": "success",
                **await service.execute(
                    key,
                    request.resolve()
                    if isinstance(request, SemanticMonthRequest)
                    else request,
                ),
            }
        except SemanticAuditError:
            return {
                "status": "error",
                "audit_status": "failed",
                "semantic_validation_status": "not_verified",
                "message": "审计写入失败",
            }
        except SQLAlchemyError:
            return {
                "status": "error",
                "semantic_validation_status": "not_verified",
                "message": "查询或持久化未完成，请检查执行记录",
            }
        except (ValueError, PermissionError) as exc:
            return tool_error("受控语义查询未完成", exc)

    @tool
    async def read_verified_evidence(execution_id: UUID):
        """按当前授权核对本分析的服务端语义执行记录，读取数字、覆盖状态与证据。"""
        try:
            return {"status": "success", **await service.read(key, execution_id)}
        except (ValueError, PermissionError, SQLAlchemyError) as exc:
            return tool_error("无法验证本次证据", exc)

    return (
        [recall_ontology, execute_semantic_query, read_verified_evidence]
        if role == "explorer"
        else [read_verified_evidence]
    )
