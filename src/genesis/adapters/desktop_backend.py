"""Explicit desktop backend selection; configuration never contacts a service."""

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Literal

from genesis.adapters.synthetic_ollama import SyntheticOllamaServer
from genesis.models.contracts import Model
from genesis.models.fake import FakeModel
from genesis.models.ollama import OllamaModel


@dataclass(frozen=True)
class BackendConfig:
    """Temporary-session settings, not persisted service configuration.

    ``local_service_verified`` acknowledges a prior independent check of the
    service and cloud setting. It is not automatic proof of local inference.
    Only an explicit ``ollama`` selection can use these endpoint/model fields;
    synthetic mode owns a separate temporary fixture and ignores them.
    """

    mode: Literal["fake", "synthetic-http", "ollama"] = "fake"
    endpoint: str = "http://127.0.0.1:11434"
    model: str = "qwen2.5:7b-instruct-q4_K_M"
    local_service_verified: bool = False

    def __post_init__(self) -> None:
        if type(self.mode) is not str or self.mode not in ("fake", "synthetic-http", "ollama"):
            raise ValueError("unsupported desktop backend")
        if type(self.local_service_verified) is not bool:
            raise TypeError("local_service_verified must be an explicit boolean acknowledgment")
        if self.mode == "ollama":
            if not self.local_service_verified:
                raise ValueError("real service requires prior local-service verification acknowledgment")
            # Reuse the production adapter's validation; construction performs
            # no discovery, socket connection, model loading or configuration.
            OllamaModel(self.endpoint, self.model)


@contextmanager
def backend_context(config: BackendConfig) -> Iterator[Model]:
    """Construct and clean up resources on the owning ChatWorker thread.

    Real mode owns no service process and changes no service setting. Its first
    network request occurs only when Core explicitly invokes ``generate``.
    No backend performs automatic retries, downloads or fallback selection.
    """
    if not isinstance(config, BackendConfig):
        raise TypeError("config must be BackendConfig")
    if config.mode == "fake":
        yield FakeModel()
    elif config.mode == "synthetic-http":
        with SyntheticOllamaServer() as server:
            yield OllamaModel(server.endpoint, "synthetic-http-v1")
    else:
        yield OllamaModel(config.endpoint, config.model)
