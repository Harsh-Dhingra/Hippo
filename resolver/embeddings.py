"""Embedding providers.

STACK.md keeps the embedding model config-abstracted so that changing it is a
resolver re-run rather than a migration, and P1-RES-3 is where the interface
lands. Two implementations ship:

**OpenAICompatibleEmbeddings** talks to any `/embeddings` endpoint that follows
the OpenAI request shape, which covers hosted providers, vLLM, Ollama and LM
Studio alike. That is the whole reason to target that shape rather than one
vendor's SDK: the self-hoster who wants their embeddings on their own hardware
changes a base URL.

**HashingEmbeddings** needs no service at all. It is the hashing trick, a
bag-of-words vector folded into the configured dimensions, so it is
deterministic, dependency-free, and gives crude lexical similarity. It is what
CI runs against and what lets someone try the project before deciding on a
model. It is emphatically not semantic: it will match "renewal" to "renewal"
and has no idea "contract" is related. Keyword retrieval through Postgres FTS
is the honest fallback, and the vector half is why configuring a real endpoint
matters.

Which model to standardise on is deliberately still open. STACK.md defers the
pick to a small eval, that eval needs an endpoint to measure, and guessing a
name here would put an unmeasured default into everyone's config.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from collections.abc import Iterator, Sequence
from typing import Protocol

import httpx

from core.config import Settings

LOG = logging.getLogger("hippo.resolver.embeddings")

DEFAULT_DIMENSIONS = 1024
DEFAULT_BATCH = 64

_TOKEN = re.compile(r"[a-z0-9]+")


class EmbeddingError(RuntimeError):
    """The embedding service could not be reached or refused the request.

    One error type on purpose: enrichment runs as a job, and the jobs runtime
    already owns retry, backoff and the dead letter. Splitting the taxonomy
    here would duplicate a decision that is made better one layer up.
    """


class EmbeddingProvider(Protocol):
    """Text in, vectors out."""

    model: str
    dimensions: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def _batched(texts: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    for start in range(0, len(texts), size):
        yield texts[start : start + size]


class HashingEmbeddings:
    """Deterministic, offline, lexical. See the module docstring for the caveat."""

    def __init__(self, dimensions: int = DEFAULT_DIMENSIONS) -> None:
        if dimensions < 1:
            msg = f"dimensions must be >= 1, got {dimensions}"
            raise ValueError(msg)
        self.model = "hashing-bow"
        self.dimensions = dimensions

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._one(text) for text in texts]

    def _one(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for token in _TOKEN.findall(text.lower()):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            bucket = int.from_bytes(digest[:4], "big") % self.dimensions
            # A sign bit from a different part of the digest, so two tokens
            # colliding in the same bucket tend to cancel rather than compound.
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[bucket] += sign

        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            # Cosine distance is undefined against a zero vector, and pgvector
            # will happily store one. Park empty text on a fixed unit vector
            # instead, which is far from everything real.
            vector[0] = 1.0
            return vector
        return [value / norm for value in vector]


class OpenAICompatibleEmbeddings:
    """Any endpoint that speaks the OpenAI /embeddings request shape."""

    def __init__(
        self,
        model: str,
        *,
        base_url: str = "https://api.openai.com/v1",
        api_key: str = "",
        dimensions: int = DEFAULT_DIMENSIONS,
        batch_size: int = DEFAULT_BATCH,
        client: httpx.Client | None = None,
        timeout: float = 60.0,
    ) -> None:
        if not model:
            msg = "an embedding model must be named; see HIPPO_EMBEDDING_MODEL"
            raise ValueError(msg)
        self.model = model
        self.dimensions = dimensions
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._batch_size = max(1, batch_size)
        self._client = client if client is not None else httpx.Client(timeout=timeout)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []

        vectors: list[list[float]] = []
        for batch in _batched(texts, self._batch_size):
            vectors.extend(self._call(batch))

        if len(vectors) != len(texts):
            msg = f"expected {len(texts)} embeddings, got {len(vectors)}"
            raise EmbeddingError(msg)
        return vectors

    def _call(self, batch: Sequence[str]) -> list[list[float]]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        try:
            response = self._client.post(
                f"{self._base_url}/embeddings",
                json={"model": self.model, "input": list(batch)},
                headers=headers,
            )
        except httpx.HTTPError as exc:
            msg = f"embedding request failed: {exc}"
            raise EmbeddingError(msg) from exc

        if response.status_code >= httpx.codes.BAD_REQUEST:
            msg = f"embedding endpoint returned HTTP {response.status_code}: {response.text[:200]}"
            raise EmbeddingError(msg)

        body = response.json()
        rows = body.get("data")
        if not isinstance(rows, list):
            msg = f"embedding response has no data array: {str(body)[:200]}"
            raise EmbeddingError(msg)

        vectors: list[list[float]] = []
        # Providers are allowed to return batches out of order; index is what
        # says which input a vector belongs to.
        for row in sorted(rows, key=lambda item: int(item.get("index", 0))):
            vector = row.get("embedding")
            if not isinstance(vector, list):
                msg = "embedding response row has no embedding"
                raise EmbeddingError(msg)
            if len(vector) != self.dimensions:
                msg = (
                    f"model {self.model!r} returned {len(vector)} dimensions, "
                    f"configured for {self.dimensions}. The chunks column is a "
                    f"fixed-width vector, so this has to match."
                )
                raise EmbeddingError(msg)
            vectors.append([float(value) for value in vector])
        return vectors


def to_pgvector(vector: Sequence[float]) -> str:
    """pgvector's text input format."""
    return "[" + ",".join(repr(float(value)) for value in vector) + "]"


def build_provider(settings: Settings) -> EmbeddingProvider:
    """The configured provider. One place that knows which is which."""
    if settings.embedding_provider == "openai":
        return OpenAICompatibleEmbeddings(
            settings.embedding_model,
            base_url=settings.embedding_base_url,
            api_key=settings.embedding_api_key,
            dimensions=settings.embedding_dimensions,
        )
    LOG.warning(
        "using the offline hashing embedder; vector search will match on shared words "
        "and nothing else. Configure HIPPO_EMBEDDING_PROVIDER=openai for real retrieval.",
        extra={"dimensions": settings.embedding_dimensions},
    )
    return HashingEmbeddings(dimensions=settings.embedding_dimensions)
