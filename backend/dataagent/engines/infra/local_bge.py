"""使用本地 BGE-M3 ONNX 权重生成 CLS 归一化向量。"""

import json
from pathlib import Path

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer


class LocalBGEEncoder:
    """复用已有 ONNX 模型，按小批次在 CPU 上推理。"""

    def __init__(
        self,
        model_path: Path,
        *,
        batch_size: int = 8,
        max_length: int = 512,
        threads: int = 2,
    ) -> None:
        if min(batch_size, max_length, threads) < 1:
            raise ValueError("批量、文本长度和线程数必须大于零")
        root = model_path.resolve(strict=True)
        pooling = json.loads((root / "1_Pooling/config.json").read_text())
        if not pooling.get("pooling_mode_cls_token") or any(
            enabled
            for key, enabled in pooling.items()
            if key.startswith("pooling_mode_") and key != "pooling_mode_cls_token"
        ):
            raise ValueError("本地 BGE 模型必须使用 CLS pooling")
        self.dimensions = pooling["word_embedding_dimension"]
        self._batch_size = batch_size
        self._tokenizer = Tokenizer.from_file(str(root / "tokenizer.json"))
        model_config = json.loads((root / "config.json").read_text())
        pad_id = model_config["pad_token_id"]
        self._tokenizer.enable_padding(
            pad_id=pad_id, pad_token=self._tokenizer.id_to_token(pad_id)
        )
        self._tokenizer.enable_truncation(max_length=max_length)
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        self._session = ort.InferenceSession(
            str(root / "onnx/model.onnx"),
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )
        self._inputs = {item.name for item in self._session.get_inputs()}
        if not self._inputs <= {"input_ids", "attention_mask", "token_type_ids"}:
            raise ValueError(f"不支持的 ONNX 模型输入: {self._inputs}")
        self._output = self._session.get_outputs()[0].name

    def encode(self, texts: list[str]) -> list[list[float]]:
        """保持输入顺序，提取 CLS 后执行 L2 归一化。"""
        result: list[list[float]] = []
        for offset in range(0, len(texts), self._batch_size):
            encoded = self._tokenizer.encode_batch(
                texts[offset : offset + self._batch_size]
            )
            values = {
                "input_ids": np.asarray([row.ids for row in encoded], dtype=np.int64),
                "attention_mask": np.asarray(
                    [row.attention_mask for row in encoded], dtype=np.int64
                ),
                "token_type_ids": np.asarray(
                    [row.type_ids for row in encoded], dtype=np.int64
                ),
            }
            hidden = self._session.run(
                [self._output], {name: values[name] for name in self._inputs}
            )[0]
            if hidden.ndim != 3 or hidden.shape[-1] != self.dimensions:
                raise ValueError(f"BGE 模型输出形状不正确: {hidden.shape}")
            vectors = hidden[:, 0, :].astype(np.float32)
            norms = np.linalg.norm(vectors, axis=1, keepdims=True)
            if not np.isfinite(vectors).all() or (norms == 0).any():
                raise ValueError("BGE 模型返回了无效向量")
            result.extend((vectors / norms).tolist())
        return result
