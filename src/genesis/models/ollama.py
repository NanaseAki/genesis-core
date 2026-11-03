"""Explicit loopback-only, non-streaming /api/chat. No discovery or retries."""
from dataclasses import dataclass, field
from enum import Enum
import http.client
import ipaddress
import json
import math
import re
import socket

from genesis.models.contracts import ModelError, ModelReply, ModelRequest, validate_model_id
from genesis.models._http_body import FramingError, read_body
from genesis.text import TextContractError


class OllamaFailure(str, Enum):
    CONNECTION_REFUSED = "connection_refused"
    CONNECTION_ERROR = "connection_error"
    CONNECT_TIMEOUT = "connect_timeout"
    READ_TIMEOUT = "read_timeout"
    HTTP_ERROR = "http_error"
    SERVER_ERROR = "server_error"
    REDIRECT = "redirect_rejected"
    INVALID_JSON = "invalid_json"
    INVALID_RESPONSE = "invalid_response"
    MODEL_MISMATCH = "model_mismatch"
    INVALID_TEXT = "invalid_text"
    INCOMPLETE = "incomplete_response"
    TOO_LARGE = "response_too_large"


class OllamaError(ModelError):
    """Safe diagnostic reason; no response body, prompt or server error text."""

    def __init__(self, reason: OllamaFailure, *, status: int | None = None) -> None:
        self.reason = reason
        self.status = status
        super().__init__(reason.value)


class OllamaDiagnostic(Exception):
    """Safe categorical cause, with no original response/exception attached."""


def _failure(reason: OllamaFailure, *, status: int | None = None,
             timeout: TimeoutError | None = None) -> OllamaError:
    result = OllamaError(reason, status=status)
    if timeout is not None:
        # Keep the useful timeout category, but never retain or mutate the
        # original exception, its payload, traceback, notes or related errors.
        result.__cause__ = TimeoutError(reason.value)
    else:
        result.__cause__ = OllamaDiagnostic(reason.value)
    return result


def _raise_public(error: OllamaError) -> None:
    # Raising even outside an inner except can attach a caller's active error.
    # Clear that context *after* the raise, then use bare re-raise (no new link).
    try:
        raise error from error.__cause__
    except OllamaError:
        error.__context__ = None
        raise


def _finite_number(value: object) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _positive_number(value: object, name: str) -> None:
    if not _finite_number(value) or value <= 0:
        raise ValueError(f"{name} must be positive and finite")


def _endpoint(value: str) -> tuple[str, int]:
    if not isinstance(value, str):
        raise ValueError("endpoint must be an explicit loopback HTTP origin")
    match = re.fullmatch(r"http://(\[[0-9a-fA-F:.]+\]|[0-9.]+):([0-9]{1,5})/?", value, re.IGNORECASE)
    if match is None:
        raise ValueError("endpoint must be an explicit loopback HTTP IP and port")
    host, port = match[1].removeprefix("[").removesuffix("]"), int(match[2])
    allowed = False
    try:
        address = ipaddress.ip_address(host)
        allowed = address.is_loopback and (address.version == 6) == match[1].startswith("[")
    except ValueError:
        pass
    if not allowed or not 1 <= port <= 65535:
        raise ValueError("endpoint must be an explicit loopback HTTP IP and port")
    return host, port


@dataclass(frozen=True)
class GenerationOptions:
    """Small explicit allowlist; no arbitrary service-specific options mapping."""
    temperature: float = 0.0
    seed: int = 0
    num_predict: int = 256

    def __post_init__(self) -> None:
        if (not _finite_number(self.temperature)
                or self.temperature < 0):
            raise ValueError("temperature must be finite and nonnegative")
        if type(self.seed) is not int or not -(2**31) <= self.seed < 2**31:
            raise ValueError("seed must be a signed 32-bit integer")
        if type(self.num_predict) is not int or not 1 <= self.num_predict <= 32768:
            raise ValueError("num_predict must be between 1 and 32768")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError("non-finite JSON constant")


@dataclass(frozen=True)
class OllamaModel:
    endpoint: str
    model: str
    options: GenerationOptions = field(default_factory=GenerationOptions)
    connect_timeout: float = 5.0
    read_timeout: float = 60.0
    max_response_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        _endpoint(self.endpoint)
        validate_model_id(self.model)
        if not isinstance(self.options, GenerationOptions):
            raise TypeError("options must be GenerationOptions")
        _positive_number(self.connect_timeout, "connect_timeout")
        _positive_number(self.read_timeout, "read_timeout")
        if type(self.max_response_bytes) is not int or not 1 <= self.max_response_bytes <= 16 * 1024 * 1024:
            raise ValueError("max_response_bytes must be between 1 and 16777216")

    @property
    def model_id(self) -> str:
        return self.model

    def generate(self, request: ModelRequest) -> ModelReply:
        """One HTTP attempt. Socket timeouts are not an end-to-end deadline.

        No DB/state handles, tools, automatic retry, proxy, redirect or fallback.
        Interruptions propagate after closing the connection. A failed call may
        nevertheless have run on the server; callers explicitly decide retries.
        """
        if not isinstance(request, ModelRequest):
            raise TypeError("request must be ModelRequest")
        host, port = _endpoint(self.endpoint)
        payload = json.dumps({
            "model": self.model,
            "messages": [{"role": m.role.value, "content": m.content} for m in request.messages],
            "stream": False,
            "options": {"temperature": self.options.temperature, "seed": self.options.seed,
                        "num_predict": self.options.num_predict},
        }, ensure_ascii=False, allow_nan=False).encode("utf-8")
        # http.client connects directly. It never consults HTTP_PROXY or follows
        # Location. An IP literal avoids hostname-based discovery/DNS selection.
        connection = http.client.HTTPConnection(host, port, timeout=self.connect_timeout)
        phase = "connect"
        failure = None
        reply = None
        try:
            connection.connect()
            assert connection.sock is not None
            connection.sock.settimeout(self.read_timeout)
            phase = "read"
            connection.request("POST", "/api/chat", body=payload,
                               headers={"Content-Type": "application/json", "Accept": "application/json", "Connection": "close"})
            with connection.getresponse() as response:
                if 300 <= response.status < 400:
                    raise OllamaError(OllamaFailure.REDIRECT, status=response.status)
                if response.status != 200:
                    raise OllamaError(OllamaFailure.SERVER_ERROR if response.status >= 500
                                      else OllamaFailure.HTTP_ERROR, status=response.status)
                if (response.headers.get_content_type() != "application/json"
                        or response.getheader("Content-Encoding", "identity").lower() != "identity"):
                    raise OllamaError(OllamaFailure.INVALID_RESPONSE)
                reply = self._decode(read_body(response, self.max_response_bytes))
        except OllamaError as error:
            failure = _failure(error.reason, status=error.status)
        except FramingError as error:
            failure = _failure(OllamaFailure(error.code))
        except ConnectionRefusedError:
            failure = _failure(OllamaFailure.CONNECTION_REFUSED)
        except (socket.timeout, TimeoutError) as error:
            failure = _failure(OllamaFailure.CONNECT_TIMEOUT if phase == "connect"
                               else OllamaFailure.READ_TIMEOUT, timeout=error)
        except http.client.IncompleteRead:
            failure = _failure(OllamaFailure.INCOMPLETE)
        except (OSError, http.client.HTTPException):
            failure = _failure(OllamaFailure.CONNECTION_ERROR)
        finally:
            connection.close()
        if failure is not None:
            _raise_public(failure)
        assert reply is not None
        return reply

    def _decode(self, body: bytes) -> ModelReply:
        try:
            data = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object,
                              parse_constant=_invalid_constant)
        except (ValueError, UnicodeError, RecursionError) as error:
            raise OllamaError(OllamaFailure.INVALID_JSON) from error
        if not isinstance(data, dict):
            raise OllamaError(OllamaFailure.INVALID_RESPONSE)
        if "error" in data:
            raise OllamaError(OllamaFailure.SERVER_ERROR)
        try:
            model = validate_model_id(data.get("model"))
        except ModelError as error:
            raise OllamaError(OllamaFailure.INVALID_RESPONSE) from error
        if model != self.model:
            raise OllamaError(OllamaFailure.MODEL_MISMATCH)
        if data.get("done") is not True or data.get("done_reason") != "stop":
            raise OllamaError(OllamaFailure.INCOMPLETE)
        message = data.get("message")
        if (not isinstance(message, dict) or message.get("role") != "assistant"
                or ("tool_calls" in message and message["tool_calls"] != [])):
            raise OllamaError(OllamaFailure.INVALID_RESPONSE)
        try:
            return ModelReply(message.get("content"))
        except TextContractError as error:
            raise OllamaError(OllamaFailure.INVALID_TEXT) from error
