"""BGE 的 CLS 归一化和本机 Embedding 接口契约。"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("onnxruntime")
pytest.importorskip("tokenizers")

from fastapi.testclient import TestClient

from dataagent.engines.infra.local_bge import LocalBGEEncoder
from scripts.serve_embedding import create_app


def test_cls_pooling_normalizes_each_vector_and_respects_model_inputs():
    encoder = LocalBGEEncoder.__new__(LocalBGEEncoder)
    encoder.dimensions = 2
    encoder._batch_size = 8
    encoder._inputs = {"input_ids", "attention_mask"}
    encoder._output = "last_hidden_state"
    encoder._tokenizer = MagicMock()
    encoder._tokenizer.encode_batch.return_value = [
        SimpleNamespace(ids=[0, 2], attention_mask=[1, 1], type_ids=[0, 0]),
        SimpleNamespace(ids=[0, 1], attention_mask=[1, 0], type_ids=[0, 0]),
    ]
    encoder._session = MagicMock()
    encoder._session.run.return_value = [
        np.asarray([[[3, 4], [100, 100]], [[0, 2], [100, 100]]], dtype=np.float32)
    ]
    np.testing.assert_allclose(encoder.encode(["销售额", "库存"]), [[0.6, 0.8], [0, 1]])
    feeds = encoder._session.run.call_args.args[1]
    assert set(feeds) == {"input_ids", "attention_mask"}
    assert feeds["input_ids"].dtype == np.int64


def test_local_api_preserves_order_and_rejects_other_models():
    encoder = MagicMock()
    encoder.dimensions = 2
    encoder.encode.return_value = [[0.6, 0.8], [0.0, 1.0]]
    with TestClient(create_app(encoder, "BAAI/bge-m3")) as client:
        response = client.post(
            "/v1/embeddings", json={"model": "BAAI/bge-m3", "input": ["销售额", "库存"]}
        )
        assert response.status_code == 200
        assert response.json()["data"] == [
            {"object": "embedding", "index": 0, "embedding": [0.6, 0.8]},
            {"object": "embedding", "index": 1, "embedding": [0.0, 1.0]},
        ]
        assert (
            client.post(
                "/v1/embeddings", json={"model": "unknown", "input": ["销售额"]}
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/v1/embeddings", json={"model": "BAAI/bge-m3", "input": []}
            ).status_code
            == 422
        )
    encoder.encode.assert_called_once_with(["销售额", "库存"])
