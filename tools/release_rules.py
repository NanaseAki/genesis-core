"""Closed release metadata/container rules, independent of candidate receipts.

Only the audited setuptools 84.1.0 pure-Python format is supported. Changing the
backend or project metadata requires reviewing these rules, not trusting a new
candidate's hashes. No candidate configuration is executed here.
"""
import hashlib
import io
import re
import struct
import tarfile
import tomllib
import zipfile
import zlib

BACKEND_VERSION = "84.1.0"
CONTAINER_LIMIT = 32 * 1024 * 1024
# Unmodified ASF https://www.apache.org/licenses/LICENSE-2.0.txt (11358 bytes).
APACHE_LICENSE_SHA256 = "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"


class ReleaseError(ValueError):
    """Safe categorical failure without source or candidate contents."""


def text(data: bytes) -> str:
    try:
        value = data.decode("utf-8").replace("\r\n", "\n")
    except UnicodeError:
        raise ReleaseError("metadata is not UTF-8") from None
    if "\r" in value or "\0" in value:
        raise ReleaseError("metadata contains unsupported controls")
    return value


def approved_licenses(files: dict[str, bytes]) -> tuple[str, ...]:
    """Only the reviewed static Apache declaration and exact ASF bytes."""
    project = tomllib.loads(files["pyproject.toml"].decode("utf-8"))["project"]
    if (project.get("license") != "Apache-2.0"
            or project.get("license-files") != ["LICENSE"]
            or "NOTICE" in files
            or not isinstance(files.get("LICENSE"), bytes)
            or hashlib.sha256(files["LICENSE"]).hexdigest() != APACHE_LICENSE_SHA256):
        raise ReleaseError("unapproved license declaration or content")
    return ("LICENSE",)


def _headers(data: bytes, expected: dict[str, str | tuple[str, ...]], body: str = "") -> None:
    header, separator, payload = text(data).partition("\n\n")
    actual: dict[str, str | tuple[str, ...]] = {}
    for line in header.split("\n"):
        key, colon, value = line.partition(": ")
        if not colon:
            raise ReleaseError("ambiguous metadata headers")
        if key == "License-File" and isinstance(expected.get(key), tuple):
            previous = actual.get(key, ())
            if not isinstance(previous, tuple) or value in previous:
                raise ReleaseError("duplicate license file header")
            actual[key] = (*previous, value)
        else:
            if key in actual:
                raise ReleaseError("ambiguous metadata headers")
            actual[key] = value
    if not separator or actual != expected or payload != body:
        raise ReleaseError("metadata differs from approved sources")


def verify_metadata(files: dict[str, bytes], wheel: dict[str, bytes],
                    source: dict[str, bytes], version: str) -> None:
    project = tomllib.loads(files["pyproject.toml"].decode("utf-8"))["project"]
    stem = "project_genesis-" + version
    info, egg = stem + ".dist-info/", stem + "/src/project_genesis.egg-info/"
    headers = {"Metadata-Version": "2.4", "Name": project["name"], "Version": version,
               "Description-Content-Type": "text/markdown",
               "License-Expression": "Apache-2.0", "License-File": approved_licenses(files),
               "Dynamic": "license-file"}
    for key, field in (("description", "Summary"), ("requires-python", "Requires-Python")):
        if key in project:
            headers[field] = project[key]
    readme = text(files["README.md"])
    if not readme.endswith("\n"):
        readme += "\n"
    for data in (wheel[info + "METADATA"], source[stem + "/PKG-INFO"], source[egg + "PKG-INFO"]):
        _headers(data, headers, readme)
    _headers(wheel[info + "WHEEL"], {
        "Wheel-Version": "1.0", "Generator": "setuptools (" + BACKEND_VERSION + ")",
        "Root-Is-Purelib": "true", "Tag": "py3-none-any"})
    scripts = project.get("scripts", {})
    if (not isinstance(scripts, dict) or any(
            not re.fullmatch(r"[a-z][a-z0-9-]*", name)
            or not isinstance(target, str)
            or not re.fullmatch(r"genesis(?:\.[a-z_][a-z0-9_]*)+:[a-z_][a-z0-9_]*", target)
            for name, target in scripts.items())):
        raise ReleaseError("unreviewed console entry configuration")
    points = ("[console_scripts]\n" + "".join(k + " = " + v + "\n" for k, v in sorted(scripts.items()))) if scripts else ""
    for data in (wheel[info + "entry_points.txt"], source[egg + "entry_points.txt"]):
        if text(data) != points:
            raise ReleaseError("console entries differ from approved configuration")
    for data in (wheel[info + "top_level.txt"], source[egg + "top_level.txt"]):
        if text(data) != "genesis\n":
            raise ReleaseError("unapproved top-level package")
    if text(source[egg + "dependency_links.txt"]) != "\n":
        raise ReleaseError("dependency links forbidden")
    if text(source[stem + "/setup.cfg"]) != "[egg_info]\ntag_build = \ntag_date = 0\n\n":
        raise ReleaseError("unapproved generated setup configuration")
    generated = {"src/project_genesis.egg-info/" + n for n in
                 ("PKG-INFO", "SOURCES.txt", "dependency_links.txt", "entry_points.txt", "top_level.txt")}
    inventory = text(source[egg + "SOURCES.txt"])
    paths = inventory.splitlines()
    if (inventory not in ("\n".join(paths), "\n".join(paths) + "\n") or len(paths) != len(set(paths))
            or set(paths) != set(files) | generated):
        raise ReleaseError("source inventory differs from approved set")


def verify_container(path) -> None:
    """Reject hidden container payloads before higher-level archive parsing.

    Deliberately supports ordinary non-ZIP64 wheels and gzip POSIX/GNU tar with
    only numeric mtime PAX records. Timestamps are not source content.
    """
    raw = path.read_bytes()
    if len(raw) > CONTAINER_LIMIT:
        raise ReleaseError("container exceeds limit")
    try:
        if path.suffix == ".whl":
            _zip(raw)
        else:
            _tar(raw, path.name.removesuffix(".gz"))
    except (UnicodeError, zipfile.BadZipFile, tarfile.TarError, zlib.error, struct.error, EOFError):
        raise ReleaseError("invalid archive framing") from None


def _zip(raw: bytes) -> None:
    if (len(raw) < 22 or raw[-22:-18] != b"PK\x05\x06"
            or raw[-2:] != b"\0\0"):
        raise ReleaseError("ZIP comments or trailing payload forbidden")
    disk, start, count, total, size, offset, comment = struct.unpack("<4H2IH", raw[-18:])
    if disk or start or count != total or comment or offset + size != len(raw) - 22:
        raise ReleaseError("unsupported ZIP directory")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        if len(archive.infolist()) > 2048 or sum(i.file_size for i in archive.infolist()) > CONTAINER_LIMIT:
            raise ReleaseError("ZIP budget exceeded")
        cursor, central = 0, offset
        for item in archive.infolist():
            if (item.header_offset != cursor or item.extra or item.comment
                    or item.flag_bits not in (0, 0x800) or item.volume
                    or item.create_system not in (0, 3)
                    or item.compress_type not in (0, 8) or item.file_size > 1024 * 1024
                    or (item.external_attr >> 16) & 0o7000):
                raise ReleaseError("unsupported ZIP member metadata")
            header = raw[cursor:cursor + 30]
            if len(header) != 30 or header[:4] != b"PK\x03\x04":
                raise ReleaseError("invalid ZIP local header")
            fields = struct.unpack("<5H3I2H", header[4:])
            _, flags, method, _, _, crc, compressed, expanded, nlen, extra = fields
            name = item.filename.encode("ascii")
            if (flags != item.flag_bits or method != item.compress_type or crc != item.CRC
                    or compressed != item.compress_size or expanded != item.file_size
                    or nlen != len(name) or extra or raw[cursor + 30:cursor + 30 + nlen] != name):
                raise ReleaseError("ZIP local and directory records disagree")
            cursor += 30 + nlen
            end = cursor + compressed
            if end > offset:
                raise ReleaseError("truncated ZIP member")
            if method == 8:
                decoder = zlib.decompressobj(-15)
                decoded = decoder.decompress(raw[cursor:end], CONTAINER_LIMIT + 1)
                if (len(decoded) != expanded or not decoder.eof or decoder.unused_data
                        or decoder.unconsumed_tail):
                    raise ReleaseError("unapproved compressed ZIP payload")
            elif compressed != expanded:
                raise ReleaseError("invalid stored ZIP member")
            cursor = end
            if raw[central:central + 4] != b"PK\x01\x02":
                raise ReleaseError("invalid ZIP directory record")
            local = raw[central:central + 46]
            if len(local) != 46:
                raise ReleaseError("truncated ZIP directory")
            # No variable extras/comments or disk/prefix payloads.
            lengths = struct.unpack("<3H", local[28:34])
            if lengths != (len(name), 0, 0) or raw[central + 46:central + 46 + len(name)] != name:
                raise ReleaseError("unapproved ZIP directory payload")
            central += 46 + len(name)
        if cursor != offset or central != len(raw) - 22 or len(archive.infolist()) != total:
            raise ReleaseError("unclaimed ZIP bytes")


def _tar(raw: bytes, gzip_name: str) -> None:
    if len(raw) < 18 or raw[:3] != b"\x1f\x8b\x08" or raw[3] not in (0, 8):
        raise ReleaseError("unsupported gzip metadata")
    if raw[3] == 8:
        end = raw.find(b"\0", 10, 512)
        if end < 0 or raw[10:end] != gzip_name.encode("ascii"):
            raise ReleaseError("unapproved gzip filename")
    decoder = zlib.decompressobj(31)
    data = decoder.decompress(raw, CONTAINER_LIMIT + 1)
    if (len(data) > CONTAINER_LIMIT or not decoder.eof or decoder.unused_data
            or decoder.unconsumed_tail or len(data) % 512):
        raise ReleaseError("invalid or oversized gzip payload")
    cursor, pending = 0, False
    while cursor < len(data):
        header = data[cursor:cursor + 512]
        if not any(header):
            if pending or len(data) - cursor < 1024 or any(data[cursor:]):
                raise ReleaseError("unclaimed tar payload")
            return
        try:
            item = tarfile.TarInfo.frombuf(header, "utf-8", "strict")
        except (tarfile.TarError, UnicodeError, ValueError):
            raise ReleaseError("invalid tar header") from None
        if item.type == tarfile.DIRTYPE and item.size != 0:
            raise ReleaseError("tar directory size must be zero")
        if (item.size < 0 or item.size > 1024 * 1024 or item.uid or item.gid
                or item.uname not in ("", "root") or item.gname not in ("", "root")
                or item.linkname or item.mode not in ((0,) if item.type == tarfile.XHDTYPE
                                                     else (0o644, 0o666, 0o755, 0o777))):
            raise ReleaseError("unsupported tar metadata")
        for start, stop in ((0, 100), (157, 257), (265, 297), (297, 329), (345, 500)):
            field = header[start:stop]
            if b"\0" in field and any(field[field.index(b"\0"):]):
                raise ReleaseError("hidden tar header payload")
        if any(header[500:]):
            raise ReleaseError("unsupported tar header extension")
        end = cursor + 512 + item.size
        padded = (end + 511) // 512 * 512
        if padded > len(data) or any(data[end:padded]):
            raise ReleaseError("invalid tar padding")
        if item.type == tarfile.XHDTYPE:
            payload = data[cursor + 512:end]
            match = re.fullmatch(rb"([0-9]+) mtime=[0-9]{1,20}(?:\.[0-9]{1,9})?\n", payload)
            if pending or not match or int(match[1]) != len(payload):
                raise ReleaseError("unsupported tar extended metadata")
            pending = True
        elif item.type in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE):
            pending = False
        else:
            raise ReleaseError("unsupported tar record type")
        cursor = padded
    raise ReleaseError("tar terminator missing")
