from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from .models import AlbumDetail, AlbumSummary, SongDetail, SongSummary

BASE_URL = "https://monster-siren.hypergryph.com"
TRANSIENT_STATUS_CODES = {408, 429, 500, 502, 503, 504}


class SiteError(RuntimeError):
    """Raised when the Monster Siren service cannot provide valid data."""


Sleep = Callable[[float], Awaitable[None]]


class MonsterSirenClient:
    """Small typed interface over the Monster Siren JSON endpoints."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        max_retries: int = 3,
        backoff_base: float = 0.25,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._http = http
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._sleep = sleep

    async def fetch_catalog(self) -> tuple[tuple[AlbumSummary, ...], tuple[SongSummary, ...]]:
        albums_data, songs_data = await asyncio.gather(
            self._get_json("/api/albums"),
            self._get_json("/api/songs"),
        )
        albums = tuple(
            AlbumSummary(
                cid=_string(item, "cid"),
                name=_string(item, "name"),
                cover_url=_string(item, "coverUrl"),
                artists=_strings(item, "artistes"),
                release_rank=index,
            )
            for index, item in enumerate(_objects(albums_data))
        )
        songs_object = _object(songs_data)
        songs = tuple(
            SongSummary(
                cid=_string(item, "cid"),
                name=_string(item, "name"),
                album_cid=_string(item, "albumCid"),
                artists=_strings(item, "artists"),
            )
            for item in _objects(songs_object.get("list"))
        )
        return albums, songs

    async def fetch_album(self, cid: str) -> AlbumDetail:
        data = _object(await self._get_json(f"/api/album/{cid}/detail"))
        album_cid = _string(data, "cid")
        return AlbumDetail(
            cid=album_cid,
            name=_string(data, "name"),
            intro=_optional_string(data, "intro") or "",
            cover_url=_string(data, "coverUrl"),
            songs=tuple(
                SongSummary(
                    cid=_string(item, "cid"),
                    name=_string(item, "name"),
                    album_cid=album_cid,
                    artists=_strings(item, "artistes"),
                )
                for item in _objects(data.get("songs"))
            ),
        )

    async def fetch_song(self, cid: str) -> SongDetail:
        data = _object(await self._get_json(f"/api/song/{cid}"))
        return SongDetail(
            cid=_string(data, "cid"),
            name=_string(data, "name"),
            album_cid=_string(data, "albumCid"),
            source_url=_string(data, "sourceUrl"),
            lyric_url=_optional_string(data, "lyricUrl"),
            artists=_strings(data, "artists"),
        )

    async def _get_json(self, path: str) -> Any:
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._http.get(f"{BASE_URL}{path}")
                if response.status_code in TRANSIENT_STATUS_CODES:
                    raise _TransientResponse(response)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise SiteError(f"{path} 返回的 JSON 根节点不是对象")
                if payload.get("code") != 0:
                    raise SiteError(f"{path} 返回错误: {payload.get('msg') or payload.get('code')}")
                return payload.get("data")
            except SiteError:
                raise
            except httpx.HTTPStatusError as exc:
                raise SiteError(f"请求 {path} 失败: HTTP {exc.response.status_code}") from exc
            except (_TransientResponse, httpx.RequestError, ValueError) as exc:
                last_error = exc
                if attempt >= self._max_retries:
                    break
                delay = retry_delay(exc, attempt, self._backoff_base)
                await self._sleep(delay)
        raise SiteError(f"请求 {path} 失败: {last_error}") from last_error


class _TransientResponse(Exception):
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        super().__init__(f"HTTP {response.status_code}")


def retry_delay(error: Exception, attempt: int, backoff_base: float) -> float:
    if isinstance(error, _TransientResponse):
        value = error.response.headers.get("Retry-After")
        if value:
            try:
                return max(0.0, float(value))
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(value)
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=UTC)
                    now = datetime.now(UTC)
                    return max(0.0, (retry_at - now).total_seconds())
                except (TypeError, ValueError, OverflowError):
                    pass
    return backoff_base * (2**attempt)


def retry_delay_for_response(response: httpx.Response, attempt: int, backoff_base: float) -> float:
    return retry_delay(_TransientResponse(response), attempt, backoff_base)


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SiteError("官网数据结构发生变化：预期对象")
    return value


def _objects(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise SiteError("官网数据结构发生变化：预期对象列表")
    return value


def _string(item: dict[str, Any], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value:
        raise SiteError(f"官网数据结构发生变化：{key} 不是非空字符串")
    return value


def _optional_string(item: dict[str, Any], key: str) -> str | None:
    value = item.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise SiteError(f"官网数据结构发生变化：{key} 不是字符串或 null")
    return value


def _strings(item: dict[str, Any], key: str) -> tuple[str, ...]:
    value = item.get(key)
    if not isinstance(value, list) or not all(isinstance(part, str) for part in value):
        raise SiteError(f"官网数据结构发生变化：{key} 不是字符串列表")
    return tuple(value)
