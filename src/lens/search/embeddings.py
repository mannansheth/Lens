"""Local ONNX embeddings behind a narrow provider protocol."""

from collections.abc import Sequence
from typing import Protocol
import math
from lens.core.config import Settings
from lens.core.logging import event


class EmbeddingProvider(Protocol):
    model: str
    dimension: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class LocalEmbeddingProvider:
    def __init__(self, settings: Settings):
        from fastembed import TextEmbedding

        if settings.embedding_device != "cpu":
            raise ValueError("Lens supports EMBEDDING_DEVICE=cpu only")
        supported = {m["model"]: m for m in TextEmbedding.list_supported_models()}
        if settings.embedding_model not in supported:
            raise ValueError("Unsupported EMBEDDING_MODEL; choose a FastEmbed dense text model")
        self.model = settings.embedding_model
        self.dimension = supported[self.model]["dim"]
        try:
            self._encoder = TextEmbedding(
                model_name=self.model,
                cache_dir=settings.embedding_cache_dir,
                providers=["CPUExecutionProvider"],
                threads=4,
            )
        except Exception as error:
            raise RuntimeError(
                "Embedding model unavailable. Download weights while online, then retry; "
                "check EMBEDDING_CACHE_DIR."
            ) from error
        event("embedding_model_loaded", model=self.model, dimension=self.dimension)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        event("embeddings_started", count=len(texts), model=self.model)
        vectors = []
        for vector in self._encoder.embed(list(texts), batch_size=32):
            vectors.append(vector.tolist())
            if len(vectors) % 256 == 0 or len(vectors) == len(texts):
                event("embeddings_progress", completed=len(vectors), total=len(texts))
        validate_vectors(vectors, len(texts), self.dimension)
        event("embeddings_generated", count=len(vectors), model=self.model)
        return vectors


def validate_vectors(vectors: list[list[float]], count: int, dimension: int) -> None:
    if len(vectors) != count or any(len(v) != dimension for v in vectors):
        raise ValueError("Embedding count or dimension does not match the provider contract")
    if any(not all(math.isfinite(x) for x in v) or sum(x * x for x in v) == 0 for v in vectors):
        raise ValueError("Embeddings must be finite and non-zero")
