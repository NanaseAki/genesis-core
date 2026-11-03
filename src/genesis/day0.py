"""Versioned synthetic Day 0 payloads; no backup, signature or growth engine."""

from datetime import datetime, timezone
import hashlib
import json
import platform
import sqlite3
from uuid import UUID

from genesis import __version__


FORMAT_VERSION = 1


class Day0ValidationError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def canonical_json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def initial_state() -> dict:
    # A fresh value on every call; no mutable shared initial growth state.
    return {"name": None, "interests": None, "preferences": None,
            "personal_experiences": [], "confirmed_memories": [], "accepted_growth": [],
            "permissions": ["respond"], "external_authorizations": [], "active_tasks": []}


def make_day0(companion_id: UUID, created_utc: str) -> tuple[str, str]:
    payload = {"artifact_type": "genesis_day0", "format_version": FORMAT_VERSION,
               "schema_version": 1, "companion_id": str(companion_id),
               "created_utc": created_utc, "data_kind": "synthetic_development",
               "initial_state": initial_state(),
               "environment": {"genesis_version": __version__,
                               "python_version": platform.python_version(),
                               "sqlite_version": sqlite3.sqlite_version,
                               "os_family": platform.system(),
                               "rules_version": "m2a-v1", "prompt_template": None}}
    text = canonical_json(payload)
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()


def verify_day0(text: str, digest: str, companion_id: UUID, created_utc: str) -> None:
    """Verify canonical bytes, known shape, digest, identity and blank baseline.

    This is local consistency verification, not authenticity or trusted time.
    Someone able to replace the DB can replace BOTH payload and digest.
    """
    try:
        value = json.loads(text)
        if (canonical_json(value) != text
                or hashlib.sha256(text.encode("utf-8")).hexdigest() != digest):
            raise ValueError("noncanonical payload or digest mismatch")
        if set(value) != {"artifact_type", "format_version", "schema_version", "companion_id",
                          "created_utc", "data_kind", "initial_state", "environment"}:
            raise ValueError("unexpected payload fields")
        if (value["artifact_type"] != "genesis_day0"
                or type(value["format_version"]) is not int or value["format_version"] != FORMAT_VERSION
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or value["companion_id"] != str(companion_id)
                or value["created_utc"] != created_utc
                or value["data_kind"] != "synthetic_development"
                or value["initial_state"] != initial_state()):
            raise ValueError("identity, version or initial state mismatch")
        parsed = datetime.fromisoformat(created_utc)
        if not created_utc.endswith("Z") or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
            raise ValueError("creation time must be UTC")
        env = value["environment"]
        if (set(env) != {"genesis_version", "python_version", "sqlite_version", "os_family",
                         "rules_version", "prompt_template"}
                or env["rules_version"] != "m2a-v1" or env["prompt_template"] is not None
                or any(not isinstance(env[key], str) or not env[key]
                       for key in ("genesis_version", "python_version", "sqlite_version", "os_family"))):
            raise ValueError("invalid creation metadata")
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise Day0ValidationError("Day 0 validation failed; existing data was not rebuilt") from error
