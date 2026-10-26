"""应用边界、引擎依赖方向、公开契约及关系模型归属。"""

import ast
from pathlib import Path

from dataagent.engines.assistant.models.base import AssistantBase
from dataagent.engines.assistant.models.recall import SemanticRecallSnapshot
from dataagent.engines.identity.models.base import AuthBase
from dataagent.engines.metadata.models.base import MetaBase
from dataagent.engines.query.models.base import QueryBase
from dataagent.engines.query.models.execution import QueryExecution
from dataagent.engines.query.models.experience import QueryExperience

_PACKAGE = Path(__file__).resolve().parents[2] / "dataagent"
_ENGINES = _PACKAGE / "engines"
_DEPENDENCIES = {
    "identity": set(),
    "metadata": {"identity"},
    "query": {"identity", "metadata", "sandbox"},
    "assistant": {"identity", "metadata", "query", "sandbox"},
    "sandbox": set(),
    "workflows": {"assistant", "identity", "metadata", "query", "sandbox"},
}
_PUBLIC_EXPORTS = {}
for _domain in _DEPENDENCIES:
    _tree = ast.parse((_ENGINES / _domain / "__init__.py").read_text(encoding="utf-8"))
    _PUBLIC_EXPORTS[f"dataagent.engines.{_domain}"] = set(
        ast.literal_eval(
            next(
                node.value
                for node in _tree.body
                if isinstance(node, ast.Assign)
                and any(
                    isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
                )
            )
        )
    )


def _imports(path):
    package = ["dataagent", *path.relative_to(_PACKAGE).parts[:-1]]
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                prefix = package[: len(package) - node.level + 1]
                module = ".".join([*prefix, *module.split(".")]).rstrip(".")
            if module in _PUBLIC_EXPORTS:
                for alias in node.names:
                    yield (
                        node.lineno,
                        (
                            module
                            if alias.name in _PUBLIC_EXPORTS[module]
                            else f"{module}.{alias.name}"
                        ),
                    )
            else:
                yield node.lineno, module


def test_modules_only_use_public_capabilities_in_the_allowed_direction():
    violations = []
    for path in _ENGINES.rglob("*.py"):
        relative = path.relative_to(_ENGINES)
        # Agent implementations and the orchestrator belong to the Assistant capability.
        domain = relative.parts[0]
        if domain in {"agent", "orchestrator.py"}:
            domain = "assistant"
        for line, module in _imports(path):
            assert not module.startswith("dataagent.app"), (path, line, module)
            parts = module.split(".")
            if len(parts) < 3 or parts[:2] != ["dataagent", "engines"]:
                continue
            target = parts[2]
            if target == domain or target not in _DEPENDENCIES:
                continue
            public = ".".join(parts[3:]) in {"", "contracts", "errors"}
            if target not in _DEPENDENCIES.get(domain, set()) or not public:
                violations.append(f"{relative}:{line} imports {module}")
    assert not violations, "\n".join(violations)


def test_public_contracts_do_not_import_storage_or_application_implementations():
    for domain in ("assistant", "identity", "metadata", "query", "sandbox"):
        file = _ENGINES / domain / "contracts.py"
        paths = (
            [file]
            if file.is_file()
            else list((_ENGINES / domain / "contracts").rglob("*.py"))
        )
        for path in paths:
            for _, module in _imports(path):
                assert module.split(".")[0] not in {
                    "sqlalchemy",
                    "fastapi",
                    "elasticsearch",
                    "langchain_core",
                    "langgraph",
                }
                assert not {
                    "models",
                    "repositories",
                    "services",
                    "application",
                }.intersection(module.split("."))
                parts = module.split(".")
                if parts[:2] == ["dataagent", "engines"] and parts[2] in _DEPENDENCIES:
                    assert len(parts) >= 4 and parts[3] in {"contracts", "errors"}, (
                        path,
                        module,
                    )


def test_http_routes_do_not_access_storage_or_internal_services():
    for path in (_PACKAGE / "app/routers").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for _, module in _imports(path):
            assert not {"repositories", "management", "runtime"}.intersection(
                module.split(".")
            ), path
            if module.startswith("dataagent.engines."):
                assert "service" not in module.split("."), path
        assert not any(
            isinstance(n, ast.Attribute) and n.attr == "session" for n in ast.walk(tree)
        ), path


def test_state_reads_do_not_depend_on_runtime_creation_or_execution():
    path = _ENGINES / "assistant/sessions/state_reader.py"
    for _, module in _imports(path):
        assert module not in {
            "dataagent.engines.orchestrator",
            "dataagent.engines.assistant.execution.runtime_cache",
            "dataagent.engines.agent.specialist.invoker",
        }


def test_query_does_not_depend_on_agent_identity_or_checkpoint_details():
    for path in (_ENGINES / "query").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assert not any(
            isinstance(node, ast.Name)
            and node.id == "AgentSessionKey"
            or isinstance(node, ast.Attribute)
            and node.attr == "checkpoint_ns"
            for node in ast.walk(tree)
        ), path


def test_conversation_snapshots_and_query_models_have_their_own_registries():
    assert SemanticRecallSnapshot.metadata is AssistantBase.metadata
    assert SemanticRecallSnapshot.__tablename__ not in MetaBase.metadata.tables
    assert QueryExecution.metadata is QueryBase.metadata
    assert QueryExperience.metadata is QueryBase.metadata
    assert set(QueryBase.metadata.tables) == {
        "query_executions",
        "query_experiences",
        "query_experience_assets",
    }
    assert not set(QueryBase.metadata.tables).intersection(MetaBase.metadata.tables)


def test_business_model_registries_are_owned_by_their_modules():
    bases = {
        "identity": AuthBase,
        "metadata": MetaBase,
        "query": QueryBase,
        "assistant": AssistantBase,
    }
    assert len({id(base.metadata) for base in bases.values()}) == len(bases)
    for domain, base in bases.items():
        assert base.__module__ == f"dataagent.engines.{domain}.models.base"
        assert base.registry.mappers
        for mapper in base.registry.mappers:
            assert mapper.class_.__module__.startswith(
                f"dataagent.engines.{domain}.models."
            )
