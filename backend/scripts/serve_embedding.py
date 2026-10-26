"""启动仅监听本机的 BGE 向量服务，供后端和 Worker 共享模型。"""

import argparse
import asyncio
from pathlib import Path
from typing import Annotated

import uvicorn
import yaml
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from dataagent.engines.infra.local_bge import LocalBGEEncoder


class EmbeddingRequest(BaseModel):
    model: str
    input: list[Annotated[str, Field(min_length=1)]] = Field(
        min_length=1, max_length=64
    )


def create_app(encoder: LocalBGEEncoder, model_name: str) -> FastAPI:
    app = FastAPI(title="Local BGE embeddings")
    inference_lock = asyncio.Lock()

    @app.get("/health")
    async def health():
        return {"status": "ok", "model": model_name, "dimensions": encoder.dimensions}

    @app.post("/v1/embeddings")
    async def embeddings(request: EmbeddingRequest):
        if request.model != model_name:
            raise HTTPException(status_code=400, detail="请求的模型未加载")
        async with inference_lock:
            vectors = await asyncio.to_thread(encoder.encode, request.input)
        return {
            "object": "list",
            "model": model_name,
            "data": [
                {"object": "embedding", "index": index, "embedding": vector}
                for index, vector in enumerate(vectors)
            ],
        }

    return app


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    config = yaml.safe_load((root / "conf/app_config.yaml").read_text(encoding="utf-8"))
    embedding = config["embedding"]
    parser = argparse.ArgumentParser(description="本机 BGE-M3 向量服务")
    parser.add_argument(
        "--model-path", type=Path, default=embedding.get("local_model_path")
    )
    parser.add_argument("--port", type=int, default=7002)
    parser.add_argument("--max-length", type=int, default=512)
    args = parser.parse_args()
    if args.model_path is None:
        parser.error("请配置 embedding.local_model_path 或提供 --model-path")
    encoder = LocalBGEEncoder(args.model_path, max_length=args.max_length)
    uvicorn.run(
        create_app(encoder, embedding["model"]), host="127.0.0.1", port=args.port
    )


if __name__ == "__main__":
    main()
