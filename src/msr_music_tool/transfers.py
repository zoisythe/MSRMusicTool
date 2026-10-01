from __future__ import annotations

import asyncio
import io
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from .config import RpcConfig
from .site import TRANSIENT_STATUS_CODES, retry_delay_for_response


@dataclass(frozen=True, slots=True)
class RemoteMetadata:
    length: int | None
    content_type: str | None


def metadata(headers: httpx.Headers) -> RemoteMetadata:
    try:
        length = int(headers["Content-Length"])
        length = length if length >= 0 else None
    except (KeyError, ValueError):
        length = None
    return RemoteMetadata(length, headers.get("Content-Type"))


@dataclass(frozen=True, slots=True)
class ResourceProgress:
    resource_id: str
    song_ids: tuple[str, ...]
    label: str
    status: str
    downloaded: int = 0
    total: int | None = None
    speed: float = 0.0
    attempt: int = 0
    error: str | None = None


ResourceCallback = Callable[[ResourceProgress], None]


class DiskIO:
    """Bounded disk workers; cancellation waits for an in-flight operation before cleanup."""

    def __init__(self, workers: int) -> None:
        self._pool = ThreadPoolExecutor(max_workers=min(workers, 4), thread_name_prefix="msr-disk")
        self._slots = asyncio.Semaphore(min(workers, 4))

    async def run(self, function: Callable[..., Any], *args: Any) -> Any:
        async with self._slots:
            future = asyncio.get_running_loop().run_in_executor(self._pool, function, *args)
            try:
                return await asyncio.shield(future)
            except asyncio.CancelledError:
                result = await future
                # If cancellation arrived during open(), the caller never receives the
                # handle. Close it before its finally block tries to delete the temp file.
                if isinstance(result, io.IOBase):
                    await asyncio.get_running_loop().run_in_executor(self._pool, result.close)
                raise

    def close(self) -> None:
        self._pool.shutdown(wait=True)


class TransferBackend(Protocol):
    async def prepare(self) -> None: ...

    async def transfer(
        self,
        url: str,
        temp: Path,
        key: str,
        songs: tuple[str, ...],
        label: str,
        emit: ResourceCallback,
    ) -> RemoteMetadata: ...

    async def close(self) -> tuple[str, ...]: ...

    def can_delete(self, temp: Path) -> bool: ...


class HttpBackend:
    def __init__(
        self,
        http: httpx.AsyncClient,
        disk: DiskIO,
        concurrency: int,
        max_retries: int = 3,
        backoff_base: float = 0.25,
        sleep: Callable[..., Any] = asyncio.sleep,
    ) -> None:
        self.http = http
        self.disk = disk
        self.slots = asyncio.Semaphore(concurrency)
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.sleep = sleep

    async def prepare(self) -> None:
        pass

    async def close(self) -> tuple[str, ...]:
        return ()

    def can_delete(self, temp: Path) -> bool:
        return True

    async def transfer(
        self,
        url: str,
        temp: Path,
        key: str,
        songs: tuple[str, ...],
        label: str,
        emit: ResourceCallback,
    ) -> RemoteMetadata:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            emit(ResourceProgress(key, songs, label, "waiting", attempt=attempt))
            retry_response = None
            try:
                async with self.slots:
                    await self.disk.run(temp.unlink, True)
                    async with self.http.stream(
                        "GET", url, headers={"Accept-Encoding": "identity"}
                    ) as response:
                        if response.status_code in TRANSIENT_STATUS_CODES:
                            retry_response = response
                            raise OSError(f"HTTP {response.status_code}")
                        response.raise_for_status()
                        info = metadata(response.headers)
                        downloaded = 0
                        start = sample_time = time.monotonic()
                        sample_bytes = 0
                        speed = 0.0
                        emit(
                            ResourceProgress(
                                key, songs, label, "active", total=info.length, attempt=attempt
                            )
                        )
                        file = await self.disk.run(temp.open, "wb")
                        try:
                            async for chunk in response.aiter_bytes():
                                await self.disk.run(file.write, chunk)
                                downloaded += len(chunk)
                                now = time.monotonic()
                                elapsed = now - sample_time
                                if elapsed >= 0.2:
                                    speed = (downloaded - sample_bytes) / elapsed
                                    sample_time, sample_bytes = now, downloaded
                                elif speed == 0:
                                    speed = downloaded / max(now - start, 0.001)
                                emit(
                                    ResourceProgress(
                                        key,
                                        songs,
                                        label,
                                        "active",
                                        downloaded,
                                        info.length,
                                        speed,
                                        attempt,
                                    )
                                )
                        finally:
                            await self.disk.run(file.close)
                    size = await self.disk.run(lambda: temp.stat().st_size)
                    if info.length is not None and size != info.length:
                        raise OSError(f"下载长度不符：预期 {info.length}，实际 {size}")
                    emit(
                        ResourceProgress(
                            key,
                            songs,
                            label,
                            "transferred",
                            size,
                            info.length if info.length is not None else size,
                            attempt=attempt,
                        )
                    )
                    return info
            except httpx.HTTPStatusError as exc:
                last_error = exc
                break
            except (httpx.RequestError, OSError) as exc:
                last_error = exc
                if attempt == self.max_retries:
                    break
                emit(
                    ResourceProgress(
                        key, songs, label, "retrying", attempt=attempt + 1, error=str(exc)
                    )
                )
                delay = (
                    retry_delay_for_response(retry_response, attempt, self.backoff_base)
                    if retry_response is not None
                    else self.backoff_base * 2**attempt
                )
                await self.sleep(delay)
        raise OSError(f"下载失败: {last_error}") from last_error


class RpcError(OSError):
    def __init__(self, code: int, message: str) -> None:
        self.code = code
        super().__init__(message)


class Aria2Backend:
    def __init__(
        self,
        config: RpcConfig,
        disk: DiskIO,
        concurrency: int,
        output_dir: Path,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.config, self.disk, self.output_dir = config, disk, output_dir
        self.slots = asyncio.Semaphore(concurrency)
        # RPC must not inherit media proxies or expose the secret through HTTP diagnostics.
        self.http = http or httpx.AsyncClient(timeout=10, trust_env=False)
        self._owns_http = http is None
        self.tasks: dict[str, Path] = {}
        self.unsafe: set[Path] = set()
        self.broken = False

    def _safe(self, text: str) -> str:
        return text.replace(self.config.secret, "[REDACTED]") if self.config.secret else text

    async def _call(self, method: str, *params: Any) -> Any:
        values = list(params)
        if self.config.secret:
            values.insert(0, f"token:{self.config.secret}")
        try:
            response = await self.http.post(
                self.config.endpoint,
                json={
                    "jsonrpc": "2.0",
                    "id": uuid.uuid4().hex,
                    "method": method,
                    "params": values,
                },
            )
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ConnectionError(f"RPC 通信失败：{self._safe(str(exc))}") from None
        if not isinstance(data, dict):
            raise RpcError(-1, "RPC 响应不是对象")
        if "error" in data:
            error = data["error"]
            if not isinstance(error, dict):
                raise RpcError(-1, "RPC 错误响应格式无效")
            raise RpcError(error.get("code", -1), self._safe(str(error.get("message", "RPC 错误"))))
        if "result" not in data:
            raise RpcError(-1, "RPC 响应缺少 result")
        return data["result"]

    async def _read(
        self,
        method: str,
        *params: Any,
        on_retry: Callable[[int], None] | None = None,
    ) -> Any:
        for attempt in range(4):
            try:
                return await self._call(method, *params)
            except ConnectionError:
                if attempt == 3:
                    raise
                if on_retry:
                    on_retry(attempt + 1)
                await asyncio.sleep(0.25 * 2**attempt)

    async def prepare(self) -> None:
        version = await self._read("aria2.getVersion")
        if not isinstance(version, dict) or "version" not in version:
            raise RpcError(-1, "引擎未返回有效版本信息")

    def can_delete(self, temp: Path) -> bool:
        return temp not in self.unsafe and temp not in self.tasks.values()

    async def _remove(self, gid: str) -> None:
        # Query first: terminal tasks are no longer writing and cannot be forceRemoved.
        status = await self._call("aria2.tellStatus", gid, ["status"])
        if status.get("status") not in ("complete", "error", "removed"):
            await self._call("aria2.forceRemove", gid)
            while True:
                status = await self._call("aria2.tellStatus", gid, ["status"])
                if status.get("status") in ("complete", "error", "removed"):
                    break
                await asyncio.sleep(0.05)
        path = self.tasks.pop(gid)
        self.unsafe.discard(path)

    async def close(self) -> tuple[str, ...]:
        pending = tuple(self.tasks)
        if pending:
            removals = [asyncio.create_task(self._remove(gid)) for gid in pending]
            done, remaining = await asyncio.wait(removals, timeout=5)
            for task in remaining:
                task.cancel()
            await asyncio.gather(*removals, return_exceptions=True)
            for task in done:
                if not task.cancelled():
                    task.exception()
            for path in self.tasks.values():
                self.unsafe.add(path)
        unresolved = tuple(self.tasks)
        if self._owns_http:
            await self.http.aclose()
        return unresolved

    async def transfer(
        self,
        url: str,
        temp: Path,
        key: str,
        songs: tuple[str, ...],
        label: str,
        emit: ResourceCallback,
    ) -> RemoteMetadata:
        emit(ResourceProgress(key, songs, label, "waiting"))
        async with self.slots:
            if self.broken:
                raise ConnectionError("RPC 连接状态不确定，已停止提交后续任务")
            gid = uuid.uuid4().hex[:16]
            self.tasks[gid] = temp
            directory = self.config.directory or str(self.output_dir.resolve())
            options = {
                "gid": gid,
                "dir": directory,
                "out": temp.name,
                "auto-file-renaming": "false",
                "allow-overwrite": "true",
            }
            try:

                def retrying(attempt: int) -> None:
                    emit(
                        ResourceProgress(
                            key,
                            songs,
                            label,
                            "retrying",
                            attempt=attempt,
                            error="RPC 连接中断，正在重新查询原任务",
                        )
                    )

                # Never blindly retry an ambiguous addUri: recover the predetermined GID.
                try:
                    submitted = await self._call("aria2.addUri", [url], options)
                    if submitted != gid:
                        raise RpcError(-1, "引擎未采用预分配 GID")
                except RpcError:
                    self.tasks.pop(gid)
                    raise
                except ConnectionError:
                    await self._read("aria2.tellStatus", gid, ["gid", "status"], on_retry=retrying)
                while True:
                    status = await self._read(
                        "aria2.tellStatus",
                        gid,
                        [
                            "gid",
                            "status",
                            "totalLength",
                            "completedLength",
                            "downloadSpeed",
                            "errorCode",
                            "errorMessage",
                        ],
                        on_retry=retrying,
                    )
                    if not isinstance(status, dict):
                        raise RpcError(-1, "无效的下载状态")
                    state = status.get("status")
                    if state not in ("active", "waiting", "paused", "complete", "error", "removed"):
                        raise RpcError(-1, "未知的引擎任务状态")
                    total = int(status.get("totalLength", 0))
                    downloaded = int(status.get("completedLength", 0))
                    speed = float(status.get("downloadSpeed", 0))
                    emit(
                        ResourceProgress(
                            key,
                            songs,
                            label,
                            "transferred" if state == "complete" else state,
                            downloaded,
                            total if total > 0 else None,
                            speed,
                        )
                    )
                    if state in ("error", "removed"):
                        self.tasks.pop(gid)
                        raise RpcError(
                            -1,
                            self._safe(
                                f"引擎下载失败 {status.get('errorCode', '')}: "
                                f"{status.get('errorMessage', state)}"
                            ),
                        )
                    if state == "complete":
                        self.tasks.pop(gid)
                        size = await self.disk.run(lambda: temp.stat().st_size)
                        if size != downloaded or (total > 0 and size != total):
                            raise OSError("引擎文件长度不符，请检查 rpc.directory 共享目录映射")
                        return RemoteMetadata(size, None)
                    await asyncio.sleep(self.config.poll_interval)
            except (Exception, asyncio.CancelledError):
                if gid in self.tasks:
                    # A single bounded batch cleanup runs in close(), including on Ctrl+C.
                    self.unsafe.add(temp)
                    self.broken = True
                raise
