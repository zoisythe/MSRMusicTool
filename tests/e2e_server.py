"""Real local HTTP/RPC fixture with throttled synthetic media; never stores official music."""

from __future__ import annotations

import json
import socket
import threading
import time
from collections import Counter
from contextlib import AbstractContextManager, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

SONGS = [
    ("111111", "First", "a1"),
    ("222222", "Second", "a1"),
    ("333333", "First", "a2"),
    ("444444", "Unknown length", "a1"),
    ("555555", "Retry", "a1"),
    ("666666", "Broken", "a1"),
    ("777777", "Long", "a1"),
]


class FixtureServer(AbstractContextManager):
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.calls: Counter[tuple[str, str]] = Counter()
        self.active = self.max_active = 0
        self.started = threading.Event()
        self.media_size = 256 * 1024
        self.delay = 0.025
        self.jobs: dict[str, dict[str, Any]] = {}
        self.secret = "acceptance-secret"
        self.fail_polls = 0
        self.lose_add = False
        self.rpc_target: str | None = None
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def handle(self) -> None:
                # Connection resets are expected in interrupt/reconnect acceptance.
                with suppress(OSError):
                    super().handle()

            def log_message(self, *_args: object) -> None:
                pass

            def send_json(self, data: Any) -> None:
                body = json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def do_HEAD(self) -> None:
                self.media(head=True)

            def do_GET(self) -> None:
                with fixture.lock:
                    fixture.calls[("GET", self.path)] += 1
                if self.path.startswith("/api/"):
                    songs = [
                        {
                            "cid": cid,
                            "name": name,
                            "albumCid": album,
                            "artists": ["MSR"],
                            "artistes": ["MSR"],
                        }
                        for cid, name, album in SONGS
                    ]
                    if self.path == "/api/albums":
                        data = [
                            {
                                "cid": cid,
                                "name": "Album " + cid,
                                "artistes": ["MSR"],
                                "coverUrl": fixture.url + f"/{cid}.jpg",
                            }
                            for cid in ("a1", "a2")
                        ]
                    elif self.path == "/api/songs":
                        data = {"list": songs}
                    elif self.path.startswith("/api/album/"):
                        cid = self.path.split("/")[3]
                        data = {
                            "cid": cid,
                            "name": "Album " + cid,
                            "intro": "fallback intro",
                            "coverUrl": fixture.url + f"/{cid}.jpg",
                            "songs": [song for song in songs if song["albumCid"] == cid],
                        }
                    else:
                        cid = self.path.rsplit("/", 1)[-1]
                        song = next(song for song in songs if song["cid"] == cid)
                        data = {
                            **song,
                            "sourceUrl": fixture.url + f"/{cid}.wav",
                            "lyricUrl": fixture.url + "/111111.lrc" if cid == "111111" else None,
                        }
                    self.send_json({"code": 0, "data": data})
                else:
                    self.media(head=False)

            def media(self, *, head: bool) -> None:
                if head:
                    with fixture.lock:
                        fixture.calls[("HEAD", self.path)] += 1
                if self.path == "/666666.wav" or (
                    not head
                    and self.path == "/555555.wav"
                    and fixture.calls[("GET", self.path)] == 1
                ):
                    self.send_response(404 if self.path == "/666666.wav" else 503)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                is_audio = self.path.endswith(".wav")
                body = (
                    b"[00:01] lyric"
                    if self.path.endswith(".lrc")
                    else (
                        b"synthetic-cover"
                        if not is_audio
                        else b"x"
                        * (8 * 1024 * 1024 if self.path == "/777777.wav" else fixture.media_size)
                    )
                )
                unknown = self.path == "/444444.wav"
                offset = 0
                if "Range" in self.headers and not unknown:
                    offset = int(self.headers["Range"].split("=")[1].split("-")[0])
                self.send_response(206 if offset else 200)
                self.send_header("Content-Type", "audio/wav" if is_audio else "image/jpeg")
                if not unknown:
                    self.send_header("Content-Length", str(len(body) - offset))
                else:
                    self.send_header("Connection", "close")
                    self.close_connection = True
                if offset:
                    self.send_header("Content-Range", f"bytes {offset}-{len(body) - 1}/{len(body)}")
                self.end_headers()
                if head:
                    return
                with fixture.lock:
                    fixture.active += 1
                    fixture.max_active = max(fixture.max_active, fixture.active)
                fixture.started.set()
                counted = True
                try:
                    for position in range(offset, len(body), 8192):
                        if is_audio:
                            time.sleep(fixture.delay)
                        # Once the final chunk is released the client may immediately start
                        # another request. Do not count server-side post-send housekeeping.
                        if position + 8192 >= len(body):
                            with fixture.lock:
                                fixture.active -= 1
                            counted = False
                        self.wfile.write(body[position : position + 8192])
                        self.wfile.flush()
                except (OSError, ConnectionError):
                    pass
                finally:
                    if counted:
                        with fixture.lock:
                            fixture.active -= 1

            def do_POST(self) -> None:
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                method, params = request["method"], request.get("params", [])
                with fixture.lock:
                    fixture.calls[("RPC", method)] += 1
                if params and params[0] == "token:" + fixture.secret:
                    params = params[1:]
                else:
                    self.send_json(
                        {"id": request["id"], "error": {"code": 1, "message": "Unauthorized"}}
                    )
                    return
                if method == "aria2.tellStatus" and fixture.fail_polls:
                    fixture.fail_polls -= 1
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.close_connection = True
                    return
                if fixture.rpc_target:
                    payload = httpx.post(fixture.rpc_target, json=request, timeout=15).json()
                else:
                    payload = fixture.rpc(method, params)
                    payload = {"jsonrpc": "2.0", "id": request["id"], **payload}
                if method == "aria2.addUri" and fixture.lose_add:
                    fixture.lose_add = False
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.close_connection = True
                    return
                self.send_json(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def rpc(self, method: str, params: list[Any]) -> dict[str, Any]:
        if method == "aria2.getVersion":
            return {"result": {"version": "fixture"}}
        if method == "aria2.addUri":
            urls, options = params
            gid = options["gid"]
            if gid in self.jobs:
                return {"error": {"code": 1, "message": "duplicate GID"}}
            job = {
                "gid": gid,
                "status": "active",
                "totalLength": "0",
                "completedLength": "0",
                "downloadSpeed": "0",
                "stop": threading.Event(),
            }
            self.jobs[gid] = job

            def download() -> None:
                start = time.monotonic()
                path = Path(options["dir"]) / options["out"]
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with httpx.stream("GET", urls[0], timeout=15, trust_env=False) as response:
                        response.raise_for_status()
                        job["totalLength"] = response.headers.get("Content-Length", "0")
                        size = 0
                        with path.open("wb") as file:
                            for chunk in response.iter_bytes():
                                if job["stop"].is_set():
                                    break
                                file.write(chunk)
                                size += len(chunk)
                                job["completedLength"] = str(size)
                                job["downloadSpeed"] = str(
                                    int(size / max(time.monotonic() - start, 0.001))
                                )
                    job["status"] = "removed" if job["stop"].is_set() else "complete"
                except Exception as exc:
                    job.update(status="error", errorCode="1", errorMessage=str(exc))
                job["downloadSpeed"] = "0"

            worker = threading.Thread(target=download, daemon=True)
            job["worker"] = worker
            worker.start()
            return {"result": gid}
        gid = params[0]
        if gid not in self.jobs:
            return {"error": {"code": 1, "message": "GID not found"}}
        job = self.jobs[gid]
        if method == "aria2.forceRemove":
            job["stop"].set()
            job["worker"].join(2)
            return {"result": gid}
        if method == "aria2.tellStatus":
            return {"result": {key: value for key, value in job.items() if isinstance(value, str)}}
        return {"error": {"code": -1, "message": "unknown method"}}

    def __enter__(self) -> FixtureServer:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        for job in self.jobs.values():
            job["stop"].set()
        for job in self.jobs.values():
            job["worker"].join(2)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
