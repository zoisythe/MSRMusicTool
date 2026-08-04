from __future__ import annotations

import mimetypes
import re
import unicodedata
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlparse

from .catalog import Catalog
from .models import SongSummary

MAX_FILENAME_BYTES = 240
PREFIX = "塞壬唱片-MSR - "
_INVALID_TRANSLATION = str.maketrans(
    {
        "<": "＜",
        ">": "＞",
        ":": "：",
        '"': "＂",
        "/": "／",
        "\\": "＼",
        "|": "｜",
        "?": "？",
        "*": "＊",
    }
)
_VALID_EXTENSION = re.compile(r"^\.[a-zA-Z0-9]{1,8}$")
_MIME_EXTENSIONS = {
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/flac": ".flac",
    "audio/ogg": ".ogg",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "text/plain": ".txt",
}


def sanitize_component(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    normalized = "".join(" " if ord(char) < 32 else char for char in normalized)
    normalized = normalized.translate(_INVALID_TRANSLATION).strip().rstrip(". ")
    return normalized or "未命名"


def extension_from_remote(
    url: str,
    content_type: str | None = None,
    *,
    default: str = ".bin",
) -> str:
    suffix = Path(unquote(urlparse(url).path)).suffix.lower()
    if _VALID_EXTENSION.fullmatch(suffix):
        return ".jpg" if suffix == ".jpeg" else suffix
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    mapped = _MIME_EXTENSIONS.get(mime) or mimetypes.guess_extension(mime, strict=False)
    if mapped and _VALID_EXTENSION.fullmatch(mapped):
        return ".jpg" if mapped == ".jpe" else mapped
    return default


class FileNamer:
    def __init__(self, catalog: Catalog, *, max_bytes: int = MAX_FILENAME_BYTES) -> None:
        self._catalog = catalog
        self._max_bytes = max_bytes
        keys = [sanitize_component(song.name).casefold() for song in catalog.songs]
        self._name_counts = Counter(keys)

    def stem(self, song: SongSummary) -> str:
        title = sanitize_component(song.name)
        duplicate = self._name_counts[title.casefold()] > 1
        suffix = ""
        if duplicate:
            album = self._catalog.album_by_cid.get(song.album_cid)
            album_name = sanitize_component(album.name if album else song.album_cid)
            suffix = f" [{album_name}-{song.cid}]"
        return f"{PREFIX}{title}{suffix}"

    def filename(self, song: SongSummary, extension: str) -> str:
        extension = extension.lower()
        if not extension.startswith("."):
            extension = f".{extension}"
        stem = self.stem(song)
        candidate = f"{stem}{extension}"
        if len(candidate.encode("utf-8")) <= self._max_bytes:
            return candidate

        title = sanitize_component(song.name)
        album = self._catalog.album_by_cid.get(song.album_cid)
        conflict = self._name_counts[title.casefold()] > 1
        if conflict:
            album_name = sanitize_component(album.name if album else song.album_cid)
            fixed = f"{PREFIX} [-{song.cid}]{extension}"
            variable_budget = max(0, self._max_bytes - len(fixed.encode()))
            album_name = _shorten_middle(album_name, variable_budget // 2)
            stable_suffix = f" [{album_name}-{song.cid}]"
        else:
            stable_suffix = f" ~{song.cid}"
        available = self._max_bytes - len(f"{PREFIX}{stable_suffix}{extension}".encode())
        shortened = _shorten_middle(title, max(0, available))
        return f"{PREFIX}{shortened}{stable_suffix}{extension}"


def _shorten_middle(value: str, byte_budget: int) -> str:
    if len(value.encode("utf-8")) <= byte_budget:
        return value
    ellipsis = "…"
    ellipsis_bytes = len(ellipsis.encode())
    if byte_budget <= ellipsis_bytes:
        return ""

    remaining = byte_budget - ellipsis_bytes
    head_budget = remaining // 2
    tail_budget = remaining - head_budget
    head = _utf8_prefix(value, head_budget)
    tail = _utf8_suffix(value[len(head) :], tail_budget)
    return f"{head}{ellipsis}{tail}"


def _utf8_prefix(value: str, byte_budget: int) -> str:
    result: list[str] = []
    used = 0
    for char in value:
        size = len(char.encode("utf-8"))
        if used + size > byte_budget:
            break
        result.append(char)
        used += size
    return "".join(result)


def _utf8_suffix(value: str, byte_budget: int) -> str:
    result: list[str] = []
    used = 0
    for char in reversed(value):
        size = len(char.encode("utf-8"))
        if used + size > byte_budget:
            break
        result.append(char)
        used += size
    return "".join(reversed(result))
