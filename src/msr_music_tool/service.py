from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from . import __version__
from .catalog import Catalog
from .downloader import AssetOutcome, AssetResult, SongDownloadResult, SongOutcome
from .models import AlbumDetail, AlbumDownload, SongSummary
from .site import BASE_URL, MonsterSirenClient

HTTP_TIMEOUT = httpx.Timeout(60.0, connect=10.0, read=60.0, write=60.0, pool=10.0)
USER_AGENT = f"MSRMusicTool/{__version__} (+{BASE_URL}/music)"
SONG_ID = re.compile(r"[0-9]{6}\Z")


def http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=HTTP_TIMEOUT, follow_redirects=True, headers={"User-Agent": USER_AGENT}
    )


def query_identifier(query: str) -> str | None:
    query = query.strip()
    if SONG_ID.fullmatch(query):
        return query
    if "://" not in query:
        return None
    try:
        url = urlsplit(query)
        valid = (
            url.scheme in ("http", "https")
            and url.hostname == "monster-siren.hypergryph.com"
            and not url.username
            and not url.password
            and url.port in (None, 80, 443)
        )
    except ValueError:
        valid = False
    if valid:
        match = re.fullmatch(r"/(?:m/)?music/([0-9]{6})/?", url.path)
        if match:
            return match[1]
    raise ValueError(f"无效的官网歌曲链接：{query}")


def search(catalog: Catalog, queries: list[str]) -> tuple[tuple[SongSummary, ...], tuple[str, ...]]:
    results: dict[str, SongSummary] = {}
    missing: list[str] = []
    for query in queries:
        cid = query_identifier(query)
        matches = (
            ((catalog.song_by_cid[cid],) if cid in catalog.song_by_cid else ())
            if cid
            else tuple(
                song for song in catalog.songs if query.strip().casefold() in song.name.casefold()
            )
        )
        if not matches:
            missing.append(query)
        for song in matches:
            results.setdefault(song.cid, song)
    return tuple(results.values()), tuple(missing)


@dataclass(frozen=True)
class PreparedDownloads:
    albums: tuple[AlbumDownload, ...]
    failures: tuple[SongDownloadResult, ...]


async def prepare_downloads(
    site: MonsterSirenClient,
    catalog: Catalog,
    ids: list[str],
    output: Path,
    *,
    cache: dict[str, AlbumDetail] | None = None,
    concurrency: int = 4,
) -> PreparedDownloads:
    cache = cache if cache is not None else {}
    selected = set(ids)
    failures: list[SongDownloadResult] = []

    def fail(song: SongSummary, message: str) -> None:
        failures.append(
            SongDownloadResult(
                song,
                SongOutcome.FAILED,
                (AssetResult("歌曲信息", AssetOutcome.FAILED, output, message),),
            )
        )

    for cid in ids:
        if cid not in catalog.song_by_cid:
            fail(SongSummary(cid, cid, "", ()), f"歌曲 ID 不存在：{cid}")
    albums = [
        album
        for album in catalog.albums
        if any(song.cid in selected for song in catalog.songs_by_album.get(album.cid, ()))
    ]
    slots = asyncio.Semaphore(concurrency)

    async def fetch(cid: str) -> AlbumDetail:
        async with slots:
            if cid not in cache:
                cache[cid] = await site.fetch_album(cid)
            return cache[cid]

    details = await asyncio.gather(*(fetch(album.cid) for album in albums), return_exceptions=True)
    prepared = []
    found = {result.song.cid for result in failures}
    for album, detail in zip(albums, details, strict=True):
        wanted = tuple(song for song in catalog.songs_by_album[album.cid] if song.cid in selected)
        if isinstance(detail, BaseException):
            for song in wanted:
                fail(song, f"专辑信息加载失败：{detail}")
                found.add(song.cid)
        else:
            songs = tuple(song for song in detail.songs if song.cid in selected)
            found.update(song.cid for song in songs)
            if songs:
                prepared.append(AlbumDownload(detail, songs))
    for cid in ids:
        if cid not in found:
            fail(catalog.song_by_cid[cid], "官网专辑详情中缺少歌曲，请刷新索引后重试")
    return PreparedDownloads(tuple(prepared), tuple(failures))
