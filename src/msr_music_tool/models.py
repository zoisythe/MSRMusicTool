from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


@dataclass(frozen=True, slots=True)
class AlbumSummary:
    cid: str
    name: str
    cover_url: str
    artists: tuple[str, ...]
    release_rank: int


@dataclass(frozen=True, slots=True)
class SongSummary:
    cid: str
    name: str
    album_cid: str
    artists: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AlbumDetail:
    cid: str
    name: str
    intro: str
    cover_url: str
    songs: tuple[SongSummary, ...]


@dataclass(frozen=True, slots=True)
class SongDetail:
    cid: str
    name: str
    album_cid: str
    source_url: str
    lyric_url: str | None
    artists: tuple[str, ...]


class SelectionState(Enum):
    NONE = 0
    PARTIAL = 1
    FULL = 2


@dataclass(frozen=True, slots=True)
class AlbumMatch:
    album: AlbumSummary
    state: SelectionState
    matched_songs: tuple[SongSummary, ...] = ()


@dataclass(frozen=True, slots=True)
class AlbumDownload:
    album: AlbumDetail
    songs: tuple[SongSummary, ...]
