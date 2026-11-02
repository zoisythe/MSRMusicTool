from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from platformdirs import user_cache_path

from .catalog import Catalog
from .models import AlbumSummary, SongSummary


@dataclass(frozen=True, slots=True)
class CachedCatalog:
    catalog: Catalog
    fetched_at: datetime


def default_cache_path() -> Path:
    return user_cache_path("MSRMusicTool", appauthor=False) / "catalog-v1.json"


def _text(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Invalid cached text")
    return value


def _artists(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError("Invalid cached artists")
    return tuple(_text(item) for item in value)


def load_catalog_cache(path: Path) -> CachedCatalog | None:
    """Read a complete snapshot; unusable caches behave like a cache miss."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or type(raw.get("schema_version")) is not int:
            return None
        if raw["schema_version"] != 1:
            return None
        fetched_at = datetime.fromisoformat(_text(raw["fetched_at"]))
        if fetched_at.tzinfo is None:
            return None
        if not isinstance(raw["albums"], list) or not isinstance(raw["songs"], list):
            return None
        albums = []
        for item in raw["albums"]:
            if not isinstance(item, dict) or type(item["release_rank"]) is not int:
                return None
            albums.append(
                AlbumSummary(
                    _text(item["cid"]),
                    _text(item["name"]),
                    _text(item["cover_url"]),
                    _artists(item["artists"]),
                    item["release_rank"],
                )
            )
        songs = []
        for item in raw["songs"]:
            if not isinstance(item, dict):
                return None
            songs.append(
                SongSummary(
                    _text(item["cid"]),
                    _text(item["name"]),
                    _text(item["album_cid"]),
                    _artists(item["artists"]),
                )
            )
        if any(not item.cid for item in (*albums, *songs)):
            return None
        if len({a.cid for a in albums}) != len(albums):
            return None
        if len({s.cid for s in songs}) != len(songs):
            return None
        return CachedCatalog(Catalog(albums, songs), fetched_at)
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        return None


def save_catalog_cache(path: Path, snapshot: CachedCatalog) -> None:
    """Replace the previous cache only after the entire new snapshot is written."""
    payload = {
        "schema_version": 1,
        "fetched_at": snapshot.fetched_at.isoformat(),
        "albums": [asdict(album) for album in snapshot.catalog.albums],
        "songs": [asdict(song) for song in snapshot.catalog.songs],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix="catalog-",
            suffix=".tmp",
            delete=False,
        ) as file:
            temporary = Path(file.name)
            json.dump(payload, file, ensure_ascii=False, separators=(",", ":"))
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
