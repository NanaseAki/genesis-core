"""Lossless text validation shared by domain values and persistence boundaries."""


class TextContractError(ValueError):
    """Unsupported text; messages never include the rejected content."""


def validate_text(value: object, *, field: str) -> str:
    """Require nonblank, NUL-free UTF-8 text; return the original value.

    No stripping, normalization, replacement or implicit str() conversion. Lone
    surrogate code points are rejected before SQLite's encoder is invoked.
    """
    if not isinstance(value, str) or not value.strip():
        raise TextContractError(f"{field} must be a nonblank string")
    if "\x00" in value:
        raise TextContractError(f"{field} must not contain NUL")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise TextContractError(f"{field} must be valid UTF-8 text") from error
    return value
