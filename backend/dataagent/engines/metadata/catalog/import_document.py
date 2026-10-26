"""元数据批量导入服务。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import yaml
from pydantic import ValidationError as PydanticValidationError
from yaml import YAMLError

from dataagent.engines.metadata import errors as meta_error
from dataagent.engines.metadata.config import MetaConfig
from dataagent.engines.metadata.contracts import (
    ColumnKey,
)


@dataclass(frozen=True)
class ResourceChanges[T]:
    """单类元数据变更。"""

    created: list[T]
    updated: list[T]
    deleted: list[T]


@dataclass(frozen=True)
class MetaImportResult:
    """元数据导入结果。"""

    mode: ImportMode
    dry_run: bool
    tables: ResourceChanges[str]
    columns: ResourceChanges[ColumnKey]
    metrics: ResourceChanges[str]


class ImportMode(StrEnum):
    """元数据导入模式。"""

    MERGE = "merge"
    REPLACE = "replace"


def parse_metadata_yaml(content: bytes) -> MetaConfig:
    """解析并校验 UTF-8 YAML 元数据文档。"""
    if not content:
        raise meta_error.InvalidMetadataError(detail="元数据 YAML 文件不能为空")

    try:
        raw_config = yaml.safe_load(content.decode("utf-8"))
        return MetaConfig.model_validate(raw_config)
    except UnicodeDecodeError as exc:
        raise meta_error.InvalidMetadataError(
            detail="元数据 YAML 文件必须使用 UTF-8 编码",
        ) from exc
    except YAMLError as exc:
        raise meta_error.InvalidMetadataError(
            detail=f"元数据 YAML 格式解析失败: {exc}",
        ) from exc
    except PydanticValidationError as exc:
        errors = exc.errors(
            include_url=False,
            include_context=False,
            include_input=False,
        )
        raise meta_error.InvalidMetadataError(
            detail="元数据 YAML 结构不符合规范要求",
            extensions={"errors": errors},
        ) from exc
