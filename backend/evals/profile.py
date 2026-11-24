"""Read the reproducibility inputs of an explicitly configured evaluation deployment."""

import asyncio
import hashlib
import json
from functools import lru_cache
from pathlib import Path

from fastapi import HTTPException
from pydantic import SecretStr

from dataagent.app.dependencies import AnalysisUserDep, WebResourcesDep
from dataagent.engines.contract.config import cfg
from dataagent.engines.infra.embedding_client import EmbeddingClient
from dataagent.engines.prompts import SKILLS_DIRECTORY
from evals.environment import HERE, deployment_inputs, sha, snapshot


def digest(value):
    return sha(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    )


def tree(root, files):
    files = sorted(files)
    if not files:
        raise ValueError("missing profile files")
    return digest({p.relative_to(root).as_posix(): sha(p.read_bytes()) for p in files})


@lru_cache(maxsize=32)
def weight_digest(path, size, mtime_ns, ctime_ns):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def local_inputs():
    backend = HERE.parent
    prompts = backend / "dataagent/engines/prompts"
    local_model = cfg.embedding.local_model_path
    weights = None
    if local_model is not None:
        root = Path(local_model).resolve(strict=True)
        files = [
            root / k
            for k in (
                "config.json",
                "tokenizer.json",
                "1_Pooling/config.json",
                "onnx/model.onnx",
            )
        ]
        files += [
            p for p in (root / "onnx").iterdir() if p.is_file() and p not in files
        ]
        checksums = {}
        for p in files:
            st = p.stat()
            checksums[p.relative_to(root).as_posix()] = weight_digest(
                p, st.st_size, st.st_mtime_ns, st.st_ctime_ns
            )
        weights = digest(checksums)
    return {
        "prompts": tree(prompts, prompts.rglob("*.md")),
        "skills": tree(
            SKILLS_DIRECTORY,
            (
                p
                for p in SKILLS_DIRECTORY.rglob("*")
                if p.is_file() and "__pycache__" not in p.parts
            ),
        ),
        "metadata_yaml": sha((backend / "conf/meta_config.yaml").read_bytes()),
        "ontology": tree(
            backend / "conf/ontology",
            (p for p in (backend / "conf/ontology").rglob("*") if p.is_file()),
        ),
        "evaluation_code": tree(HERE, HERE.rglob("*.py")),
        "dependency_lock": sha((backend / "uv.lock").read_bytes()),
        "weights": weights,
    }


class EvaluationProfile:
    def __init__(self, username, role, database, version):
        boot = deployment_inputs()
        local = local_inputs()
        self.boot = {k: boot[k] for k in ("git", "source", "config")}
        self.boot_files = local
        self.username, self.role, self.database, self.version = (
            username,
            role,
            database,
            version,
        )

    async def read(self, user: AnalysisUserDep, resources: WebResourcesDep):
        if (
            user.is_admin
            or user.username != self.username
            or user.doris_role_name != self.role
        ):
            raise HTTPException(403, "仅限专用评测用户和角色")
        if cfg.query.learn_from_queries or cfg.doris.database != self.database:
            raise HTTPException(409, "评测库或经验写入配置不匹配")
        principal = await resources.identity.resolve_query_principal(user.id)
        scoped = cfg.doris.model_copy(
            update={
                "user": principal.query_user,
                "password": SecretStr(principal.password),
            }
        )
        state = await snapshot(fixture_config=scoped, catalog_only=True)
        if not state["tables"] or not state["es"] or not state["metadata"]:
            raise HTTPException(503, "评测依赖没有可验证的版本")
        files = await asyncio.to_thread(local_inputs)
        if self.boot_files != files or any(state[k] != v for k, v in self.boot.items()):
            raise HTTPException(409, "部署文件已变更，请重启评测进程后发布新版本")
        client = EmbeddingClient(cfg.embedding)
        try:
            [vector] = await client.aembed_documents(
                ["DataAgent evaluation profile v1"]
            )
            if len(vector) != cfg.elasticsearch.embedding_size:
                raise HTTPException(503, "嵌入服务维度不一致")
        finally:
            await client.close()
        active = cfg.lm_config.active
        selected = {
            "planner": active,
            **{
                k: active if v.model == "default" else v.model
                for k, v in cfg.agent.specialists.items()
            },
        }
        models = {
            role: {
                "route": name,
                "provider": cfg.lm_config.models[name].model_provider,
                "model": cfg.lm_config.models[name].model,
            }
            for role, name in selected.items()
        }
        sandbox = cfg.sandbox
        data = {
            "schema_version": 1,
            "profile": "dataagent_eval_readonly",
            "git": state["git"],
            "source": state["source"],
            "evaluation_code": files["evaluation_code"],
            "dependency_lock": files["dependency_lock"],
            "config": digest([state["config"], cfg.model_dump(mode="json")]),
            "prompts": files["prompts"],
            "skills": files["skills"],
            "ontology": files["ontology"],
            "metadata_yaml": files["metadata_yaml"],
            "metadata_catalog": digest(state["metadata"]),
            "indices": {
                k: {
                    "uuid": v["uuid"],
                    "docs": v["docs"],
                    "shards_digest": digest(v["seq"]),
                }
                for k, v in state["es"].items()
            },
            "models": models,
            "embedding": {
                "model": cfg.embedding.model,
                "weights": files["weights"],
                "dimensions": len(vector),
                "probe_digest": digest(vector),
            },
            "fixture": {
                "database": self.database,
                "version": self.version,
                "tables": len(state["tables"]),
                "rows": sum(t[0] for t in state["tables"].values()),
                "digest": digest(state["tables"]),
            },
            "identity": {
                "user": user.username,
                "role": principal.role_name,
                "grants_digest": principal.authorization_fingerprint,
            },
            "sandbox": {
                "image": sandbox.image,
                "memory": sandbox.memory_limit,
                "nano_cpus": sandbox.nano_cpus,
                "pids": sandbox.pids_limit,
                "max_file_bytes": sandbox.max_file_bytes,
            },
            "experience_writes": False,
        }
        return {"digest": digest(data), "manifest": data}
