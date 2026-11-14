"""Optional embedding backend for semantic recall.

Semantics here means one thing: two texts that mean the same thing but share
no words should still match ("铁钥匙" / "生锈的旧铜锁里那把钥匙"). A lexical
channel cannot do that, a vector channel can - if an embedding model is
available.

Availability is a runtime fact, not an assumption:

* `ENGINE_EMBEDDING_PROVIDER` names a provider (`openai`, `local`,
  `custom`); unset means "use whatever is configured" - the custom
  OpenAI-compatible endpoint first, then OpenAI, then a local sentence
  transformer.
* A missing key, a missing package or a dead endpoint disables the channel and
  records why; :mod:`vector_retriever` then simply has nothing to search and
  fusion uses the lexical channel alone.

The provider returns L2-normalised float vectors so cosine equals a dot product
and no downstream stage has to know about magnitudes.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

#: Well-known embedding models and their output dimension, used to validate a
#: cached index against the model that produced it.
KNOWN_DIMENSIONS: Dict[str, int] = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
    "bge-m3": 1024,
    "multilingual-e5-large": 1024,
    "paraphrase-multilingual-MiniLM-L12-v2": 384,
}

DEFAULT_MODEL = "text-embedding-3-small"
DEFAULT_LOCAL_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"


class EmbeddingUnavailable(RuntimeError):
    """No embedding backend can serve this request; the caller degrades."""


@dataclass(frozen=True)
class EmbeddingConfig:
    """Everything the provider needs, resolved from the environment."""

    provider: str = ""
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    batch_size: int = 64
    timeout_s: float = 20.0

    def effective_provider(self) -> str:
        return (self.provider or "").strip().lower()


def config_from_env() -> EmbeddingConfig:
    """Read the embedding configuration, without importing any provider."""
    explicit = os.environ.get("ENGINE_EMBEDDING_PROVIDER", "").strip().lower()
    model = os.environ.get("ENGINE_EMBEDDING_MODEL", "").strip()
    batch = _positive_int(os.environ.get("ENGINE_EMBEDDING_BATCH", ""), 64)
    timeout = _positive_float(os.environ.get("ENGINE_EMBEDDING_TIMEOUT_S", ""), 20.0)
    if explicit == "custom":
        return EmbeddingConfig(
            provider="custom",
            model=model or os.environ.get("CUSTOM_LLM_MODEL", "").strip() or DEFAULT_MODEL,
            base_url=os.environ.get("CUSTOM_LLM_BASE_URL", "").strip(),
            api_key=os.environ.get("CUSTOM_LLM_API_KEY", "").strip(),
            batch_size=batch,
            timeout_s=timeout,
        )
    if explicit == "openai":
        return EmbeddingConfig(
            provider="openai",
            model=model or DEFAULT_MODEL,
            base_url=os.environ.get("OPENAI_BASE_URL", "").strip() or "https://api.openai.com/v1",
            api_key=os.environ.get("OPENAI_API_KEY", "").strip(),
            batch_size=batch,
            timeout_s=timeout,
        )
    if explicit == "local":
        return EmbeddingConfig(
            provider="local",
            model=model or DEFAULT_LOCAL_MODEL,
            batch_size=batch,
            timeout_s=timeout,
        )
    # Unconfigured: pick the best available backend, in a fixed order so two
    # processes on the same machine never disagree about which model produced
    # an index.
    custom_key = os.environ.get("CUSTOM_LLM_API_KEY", "").strip()
    if custom_key and os.environ.get("CUSTOM_LLM_BASE_URL", "").strip():
        return EmbeddingConfig(
            provider="custom",
            model=model or os.environ.get("CUSTOM_LLM_MODEL", "").strip() or DEFAULT_MODEL,
            base_url=os.environ.get("CUSTOM_LLM_BASE_URL", "").strip(),
            api_key=custom_key,
            batch_size=batch,
            timeout_s=timeout,
        )
    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if openai_key:
        return EmbeddingConfig(
            provider="openai",
            model=model or DEFAULT_MODEL,
            base_url=os.environ.get("OPENAI_BASE_URL", "").strip() or "https://api.openai.com/v1",
            api_key=openai_key,
            batch_size=batch,
            timeout_s=timeout,
        )
    return EmbeddingConfig(provider="local", model=model or DEFAULT_LOCAL_MODEL, batch_size=batch, timeout_s=timeout)


def _positive_int(raw: str, default: int) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _positive_float(raw: str, default: float) -> float:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 and math.isfinite(value) else default


def normalise(vector: Sequence[float]) -> List[float]:
    """L2-normalise; a zero vector stays zero (and scores as "no match")."""
    norm = math.sqrt(sum(float(x) * float(x) for x in vector))
    if norm <= 0.0:
        return [0.0] * len(vector)
    return [float(x) / norm for x in vector]


class EmbeddingProvider:
    """Base class: subclasses implement :meth:`_embed_batch`."""

    name = "base"

    def __init__(self, config: Optional[EmbeddingConfig] = None):
        self.config = config or config_from_env()
        self.model = self.config.model or DEFAULT_MODEL

    @property
    def fingerprint(self) -> str:
        """Identity of the space these vectors live in.

        The index stores this string; a model change invalidates the index
        instead of silently comparing vectors from two different spaces.
        """
        raw = f"{self.name}:{self.model}".encode("utf-8")
        return hashlib.sha1(raw).hexdigest()[:16]

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        """Embed every text, in batches, keeping order. Raises on failure."""
        cleaned = [str(t or "").strip() for t in texts]
        if not any(cleaned):
            return [[] for _ in cleaned]
        out: List[List[float]] = []
        batch = max(1, self.config.batch_size)
        for start in range(0, len(cleaned), batch):
            chunk = cleaned[start : start + batch]
            vectors = self._embed_batch(chunk)
            if len(vectors) != len(chunk):
                raise EmbeddingUnavailable(
                    f"{self.name} returned {len(vectors)} vectors for {len(chunk)} texts"
                )
            out.extend(vectors)
        return out

    def _embed_batch(self, texts: Sequence[str]) -> List[List[float]]:
        raise NotImplementedError

    def available(self) -> bool:
        return bool(self.config.effective_provider())


class OpenAICompatibleProvider(EmbeddingProvider):
    """Any endpoint that speaks `POST {base}/embeddings`.

    Covers OpenAI, the custom compatible endpoint in the settings panel, and
    local servers such as llama.cpp or Ollama's OpenAI shim.
    """

    name = "openai-compatible"

    def __init__(self, config: Optional[EmbeddingConfig] = None):
        super().__init__(config)
        self.base_url = (self.config.base_url or "").rstrip("/")
        self.api_key = self.config.api_key

    def available(self) -> bool:
        return bool(self.base_url and self.api_key)

    def _embed_batch(self, texts: Sequence[str]) -> List[List[float]]:
        if not self.available():
            raise EmbeddingUnavailable("embedding endpoint or key is not configured")
        payload = json.dumps({"model": self.model, "input": list(texts)}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/embeddings",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_s) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise EmbeddingUnavailable(f"embedding endpoint returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise EmbeddingUnavailable(f"embedding endpoint unreachable: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise EmbeddingUnavailable("embedding endpoint returned non-JSON") from exc
        rows = body.get("data") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            raise EmbeddingUnavailable("embedding endpoint returned no data")
        # The API does not promise order for every compatible server, so sort
        # by the returned index instead of trusting the array order.
        ordered = sorted(rows, key=lambda row: int(row.get("index", 0)))
        vectors = []
        for row in ordered:
            vector = row.get("embedding")
            if not isinstance(vector, list) or not vector:
                raise EmbeddingUnavailable("embedding endpoint returned an empty vector")
            vectors.append(normalise([float(v) for v in vector]))
        return vectors


class LocalSentenceTransformerProvider(EmbeddingProvider):
    """sentence-transformers when installed; multilingual by default."""

    name = "sentence-transformers"

    def __init__(self, config: Optional[EmbeddingConfig] = None):
        super().__init__(config)
        self._model = None

    def available(self) -> bool:
        try:
            import sentence_transformers  # noqa: F401
        except ImportError:
            return False
        return True

    def _load(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.model)
        return self._model

    def _embed_batch(self, texts: Sequence[str]) -> List[List[float]]:
        if not self.available():
            raise EmbeddingUnavailable(
                "sentence-transformers is not installed (pip install sentence-transformers)"
            )
        model = self._load()
        vectors = model.encode(list(texts), normalize_embeddings=True)
        return [normalise([float(v) for v in row]) for row in vectors]


def build_provider(config: Optional[EmbeddingConfig] = None) -> Optional[EmbeddingProvider]:
    """The best provider this process can actually use, or None.

    `None` is a normal answer: the retrieval stack then runs lexically and
    reports the channel as degraded rather than pretending recall happened.
    """
    resolved = config or config_from_env()
    provider_name = resolved.effective_provider()
    candidates: List[EmbeddingProvider] = []
    if provider_name == "local":
        candidates = [LocalSentenceTransformerProvider(resolved)]
    elif provider_name in ("openai", "custom"):
        candidates = [OpenAICompatibleProvider(resolved)]
    elif provider_name:
        logger.warning("unknown embedding provider %r; falling back to lexical only", provider_name)
        return None
    for provider in candidates:
        if provider.available():
            return provider
    logger.info("no embedding provider available (%s); semantic recall is off", provider_name or "unset")
    return None


def probe(config: Optional[EmbeddingConfig] = None) -> Dict[str, object]:
    """Describe the embedding channel without raising, for health panels."""
    resolved = config or config_from_env()
    provider = build_provider(resolved)
    return {
        "provider": provider.name if provider else "",
        "model": provider.model if provider else "",
        "fingerprint": provider.fingerprint if provider else "",
        "available": provider is not None,
        "requested": resolved.effective_provider() or "auto",
    }


__all__ = [
    "EmbeddingConfig",
    "EmbeddingProvider",
    "EmbeddingUnavailable",
    "OpenAICompatibleProvider",
    "LocalSentenceTransformerProvider",
    "build_provider",
    "config_from_env",
    "normalise",
    "probe",
    "KNOWN_DIMENSIONS",
]
