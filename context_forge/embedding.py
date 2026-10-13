"""Optional, local-only semantic embeddings.

The provider is deliberately lazy: installing the base package never imports
sentence-transformers or downloads a model.  Callers can treat a missing
provider as lexical-only mode.
"""
from __future__ import annotations

from array import array
import hashlib
import os
from typing import Sequence

MODEL_NAME = "all-MiniLM-L6-v2"
DIMENSIONS = 384


class EmbeddingError(RuntimeError):
    pass


def pack_vector(values: Sequence[float], *, dimensions: int = DIMENSIONS) -> bytes:
    if len(values) != dimensions:
        raise ValueError(f"embedding has {len(values)} values; expected {dimensions}")
    vector = array("f", (float(value) for value in values))
    if vector.itemsize != 4:
        raise EmbeddingError("platform float size is not supported")
    if vector.typecode == "f" and __import__("sys").byteorder != "little":
        vector.byteswap()
    return vector.tobytes()


def unpack_vector(blob: bytes | None, *, dimensions: int = DIMENSIONS) -> tuple[float, ...] | None:
    if blob is None or len(blob) != dimensions * 4:
        return None
    vector = array("f")
    vector.frombytes(blob)
    if __import__("sys").byteorder != "little":
        vector.byteswap()
    return tuple(vector)


def model_identity(model: str = MODEL_NAME, *, dimensions: int = DIMENSIONS) -> str:
    return f"sentence-transformers:{model}:{dimensions}"


class LocalEmbedder:
    """Lazy sentence-transformers wrapper; never enables network access itself."""

    def __init__(self, model: str = MODEL_NAME, *, dimensions: int = DIMENSIONS) -> None:
        self.model_name = model
        self.dimensions = dimensions
        self.identity = model_identity(model, dimensions=dimensions)
        self._model = None
        self._load_error: EmbeddingError | None = None

    def _load(self):
        if self._load_error is not None:
            raise self._load_error
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                self._load_error = EmbeddingError("sentence-transformers is not installed")
                raise self._load_error from exc
            # Force offline resolution even on versions whose constructor does not
            # expose a local_files_only argument.
            previous = os.environ.get("HF_HUB_OFFLINE")
            os.environ["HF_HUB_OFFLINE"] = "1"
            try:
                self._model = SentenceTransformer(self.model_name)
            except Exception as exc:
                self._load_error = EmbeddingError(
                    f"cannot load cached local embedding model {self.model_name}")
                raise self._load_error from exc
            finally:
                if previous is None:
                    os.environ.pop("HF_HUB_OFFLINE", None)
                else:
                    os.environ["HF_HUB_OFFLINE"] = previous
        return self._model

    def encode(self, text: str) -> bytes:
        try:
            values = self._load().encode(text, convert_to_numpy=True, normalize_embeddings=True)
            return pack_vector(values.tolist(), dimensions=self.dimensions)
        except EmbeddingError:
            raise
        except Exception as exc:
            raise EmbeddingError("embedding inference failed") from exc

