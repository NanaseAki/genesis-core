"""Read only the local core.bare decision, without invoking Git or includes.

Not a general Git configuration API. Unsupported ambiguous metadata fails
closed instead of opening a possibly in-repository data directory.
"""
import re

ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def _ascii_lower(token: str) -> str:
    if not token.isascii():
        raise ValueError("unsupported non-ASCII Git decision token")
    return token.translate(ASCII_LOWER)


def _boolean(value: str | None) -> bool:
    if value is None:
        return True
    value = _ascii_lower(value)
    if value in {"true", "yes", "on"}:
        return True
    if value in {"false", "no", "off", ""}:
        return False
    match = re.fullmatch(r"([+-]?)(0x[0-9a-f]+|0[0-7]*|[1-9][0-9]*)([kmg]?)", value)
    if match:
        sign, digits, suffix = match.groups()
        base = 16 if digits.startswith("0x") else 8 if digits.startswith("0") else 10
        number = int(digits, base) * (-1 if sign == "-" else 1)
        number *= 1024 ** {"": 0, "k": 1, "m": 2, "g": 3}[suffix]
        if -(2**31) <= number < 2**31:
            return number != 0
    raise ValueError("cannot verify Git-like core.bare boolean")


def _value(source: str) -> str:
    # Preserve quoted/escaped whitespace; only unquoted surrounding whitespace
    # is insignificant. Concatenated quoted and unquoted fragments are legal.
    output: list[tuple[str, bool]] = []
    quoted = False
    index = 0
    while index < len(source):
        char = source[index]
        if char == '"':
            if not quoted:
                # Opening quotes materialize any pending spaces after a value,
                # even for an empty fragment: false "" is NOT bare=false.
                end = len(output) - 1
                while end >= 0 and not output[end][1] and output[end][0] in " \t":
                    output[end] = (output[end][0], True)
                    end -= 1
            quoted = not quoted
        elif char == "\\":
            index += 1
            if index == len(source) or source[index] not in '\\"ntb':
                raise ValueError("unsupported Git-like escape")
            output.append(({"n": "\n", "t": "\t", "b": "\b"}.get(source[index], source[index]), True))
        elif not quoted and char in "#;":
            break
        else:
            if quoted or output or char not in " \t":
                output.append((char, quoted))
        index += 1
    if quoted:
        raise ValueError("unsupported multiline Git-like quoted value")
    while output and not output[-1][1] and output[-1][0] in " \t":
        output.pop()
    start = 0
    while start < len(output) and not output[start][1] and output[start][0] in " \t":
        start += 1
    return "".join(char for char, _ in output[start:])


def bare_flag(text: str) -> bool:
    """Case-insensitive sections/keys, ordered overrides, bounded local subset.

    Supports comments, quoting, Git escapes and backslash-newline continuation.
    Rejects includes, literal multiline quoted values, invalid syntax and an
    absent bare decision. Never follows paths or reads environment Git config.
    """
    if "\x00" in text:
        raise ValueError("invalid Git-like configuration")
    # Only an unescaped trailing backslash continues the next physical line.
    records: list[str] = []
    pending: str | None = None
    physical_lines = text.split("\n")
    if text.endswith("\n"):
        physical_lines.pop()
    for physical in physical_lines:
        line = physical.removesuffix("\r")
        line = (pending or "") + line
        quoted = escaped = False
        for char in line:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = not quoted
            elif not quoted and char in "#;":
                escaped = False
                break
        if escaped:
            pending = line[:-1]
        else:
            records.append(line)
            pending = None
    if pending is not None:
        raise ValueError("incomplete Git-like continuation")
    section = ""
    result: bool | None = None
    for record in records:
        line = record.lstrip(" \t")
        if not line or line[0] in "#;":
            continue
        if line.startswith("["):
            match = re.match(r'\[([A-Za-z0-9.-]+)([ \t]+"(?:[^"\\]|\\["\\])*")?\]', line)
            if match is None:
                raise ValueError("unsupported Git-like section")
            section = _ascii_lower(match[1]) + (".subsection" if match[2] else "")
            if section.split(".")[0] in {"include", "includeif"}:
                raise ValueError("cannot verify Git-like included configuration")
            line = line[match.end():].lstrip(" \t")
            if not line or line[0] in "#;":
                continue
        option = re.match(r"([A-Za-z][A-Za-z0-9-]*)(.*)\Z", line)
        if not section or option is None:
            raise ValueError("invalid Git-like configuration item")
        tail = option[2].lstrip(" \t")
        if not tail:
            value = None
        elif tail.startswith("="):
            value = _value(tail[1:])
        else:
            raise ValueError("invalid Git-like assignment")
        if section == "core" and _ascii_lower(option[1]) == "bare":
            result = _boolean(value)
    if result is None:
        raise ValueError("cannot verify Git-like configuration without core.bare")
    return result
