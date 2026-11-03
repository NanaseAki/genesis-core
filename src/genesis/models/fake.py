"""Deterministic test adapter. No I/O, model downloads or GPU access."""

from collections.abc import Sequence

from genesis.models.contracts import ModelError, ModelReply, ModelRequest


class FakeModel:
    def __init__(self, responses: Sequence[str | ModelError] | None = None) -> None:
        self._responses = None if responses is None else tuple(responses)
        self._requests: list[ModelRequest] = []

    @property
    def model_id(self) -> str:
        return "fake-v1"

    @property
    def requests(self) -> tuple[ModelRequest, ...]:
        """In-memory call observations for synthetic tests, never a disk log."""
        return tuple(self._requests)

    def generate(self, request: ModelRequest) -> ModelReply:
        index = len(self._requests)
        self._requests.append(request)
        if self._responses is None:
            return ModelReply(f"Fake response: {request.messages[-1].content}")
        if index >= len(self._responses):
            raise ModelError("fake_script_exhausted")
        response = self._responses[index]
        if isinstance(response, ModelError):
            raise response
        return ModelReply(response)
