"""Shared parallel Range-chunk download engine for model files.

Used by both the Hugging Face and ModelScope download services. Writes
progress into the shared ``ctx.state.model_download`` state:

- aggregate ``downloaded`` always advances (sum of every tracked stream)
- per-track ``model_*`` / ``mmproj_*`` fields update when *track* is set,
  so the frontend can render one bar per concurrently downloaded file

Cancellation follows the shared ``ctx.state.model_download_cancel`` event;
chunk temp files are removed in ``finally`` per the AGENTS.md contract.
"""

import pathlib
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

from backend.context import AppContext

UrlOpen = Callable[..., Any]

CHUNK_SIZE = 32 * 1024 * 1024
READ_SIZE = 1024 * 1024
MAX_WORKERS = 8
CHUNK_RETRIES = 3


def cancel_requested(ctx: AppContext) -> bool:
    return ctx.state.model_download_cancel.is_set()


class SharedProgress:
    """Aggregates byte counts from concurrent download streams.

    Each stream (track) registers a base offset and reports deltas; the
    aggregate ``downloaded`` is the sum of all tracks plus their bases.
    Per-track totals feed the dual-bar frontend while the aggregate
    ``total`` is owned by the download worker, not this object.
    """

    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx
        self._lock = threading.Lock()
        self._bases: dict[str, int] = {}
        self._deltas: dict[str, int] = {}
        self._files: dict[str, str] = {}
        self._track_totals: dict[str, int] = {}

    def configure(self, track: str, filename: str, base: int, track_total: int) -> None:
        with self._lock:
            self._bases[track] = base
            self._files[track] = filename
            self._track_totals[track] = track_total
            self._deltas.setdefault(track, 0)

    def add(self, track: str, delta: int) -> None:
        with self._lock:
            self._deltas[track] = self._deltas.get(track, 0) + delta
            aggregate = sum(self._deltas.values())
            updates: dict[str, Any] = {"downloaded": aggregate}
            if track == "model":
                updates.update(
                    model_downloaded=self._bases.get(track, 0) + self._deltas[track],
                    model_total=self._track_totals.get(track, 0),
                    current_file=self._files.get(track, ""),
                )
            elif track == "mmproj":
                updates.update(
                    mmproj_downloaded=self._bases.get(track, 0) + self._deltas[track],
                    mmproj_total=self._track_totals.get(track, 0),
                )
            else:
                updates.update(current_file=self._files.get(track, ""))
            self._ctx.state.model_download.update(**updates)

    def done_for(self, track: str) -> int:
        with self._lock:
            return self._bases.get(track, 0) + self._deltas.get(track, 0)


class _Counter:
    """Per-stream handle: feeds deltas into a SharedProgress track."""

    def __init__(self, progress: SharedProgress, track: str) -> None:
        self._progress = progress
        self._track = track

    def add(self, count: int) -> None:
        self._progress.add(self._track, count)

    def done(self) -> int:
        return self._progress.done_for(self._track)


def _content_length(resp: Any) -> Optional[int]:
    headers = getattr(resp, "headers", None)
    raw = headers.get("Content-Length") if hasattr(headers, "get") else None
    try:
        size = int(raw)
    except (TypeError, ValueError):
        return None
    return size if size >= 0 else None


def probe_range_support(
    url: str, headers: dict[str, str], urlopen: UrlOpen
) -> tuple[bool, int]:
    """One-byte ``Range: bytes=0-0`` GET: (server honours ranges, declared total).

    ModelScope's resolve endpoint answers HEAD without Content-Length and
    Hugging Face redirects to a CDN, so a real ranged GET is the only probe
    that works for both.
    """
    request = urllib.request.Request(url, headers={**headers, "Range": "bytes=0-0"})
    try:
        with urlopen(request, timeout=30) as resp:
            status = getattr(resp, "status", 200)
            content_range = resp.headers.get("Content-Range") or ""
            accepts = str(resp.headers.get("Accept-Ranges") or "")
        if status == 206 and "/" in content_range:
            try:
                return True, int(content_range.rsplit("/", 1)[1].strip())
            except (TypeError, ValueError):
                return True, 0
        return accepts.lower() == "bytes", 0
    except (OSError, urllib.error.URLError):
        return False, 0


def _download_chunk(
    ctx: AppContext,
    url: str,
    headers: dict[str, str],
    start: int,
    end: int,
    part_path: pathlib.Path,
    counter: _Counter,
    urlopen: UrlOpen,
) -> None:
    """Fetch one byte range into *part_path*, resuming from what's on disk."""
    got = part_path.stat().st_size if part_path.exists() else 0
    span = end - start + 1
    for attempt in range(CHUNK_RETRIES):
        if cancel_requested(ctx):
            raise InterruptedError("下载已取消。")
        if got >= span:
            return
        try:
            request = urllib.request.Request(
                url,
                headers={**headers, "Range": f"bytes={start + got}-{end}"},
            )
            with urlopen(request, timeout=60) as resp:
                status = getattr(resp, "status", 200)
                if status not in (206, 200):
                    raise OSError(f"Unexpected HTTP status {status} for Range request.")
                with open(part_path, "ab" if status == 206 else "wb") as f:
                    if status == 200:
                        got = 0
                    while True:
                        if cancel_requested(ctx):
                            raise InterruptedError("下载已取消。")
                        chunk = resp.read(READ_SIZE)
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
                        counter.add(len(chunk))
            if got >= span:
                return
            raise OSError(f"Chunk short read: {got}/{span} bytes.")
        except InterruptedError:
            raise
        except Exception as exc:  # noqa: BLE001 - retried below, then reported
            if attempt + 1 >= CHUNK_RETRIES:
                raise OSError(
                    f"Chunk {start}-{end} failed after {CHUNK_RETRIES} attempts: {exc}"
                ) from exc
            time.sleep(1.5 * (attempt + 1))


def _single_stream(
    ctx: AppContext,
    url: str,
    headers: dict[str, str],
    dest: pathlib.Path,
    counter: _Counter,
    urlopen: UrlOpen,
) -> None:
    """Fallback when the server does not honour Range requests."""
    request = urllib.request.Request(url, headers=headers)
    tmp_path = dest.with_suffix(dest.suffix + ".part")
    with urlopen(request, timeout=60) as resp, open(tmp_path, "wb") as f:
        expected = _content_length(resp)
        done = 0
        while True:
            if cancel_requested(ctx):
                raise InterruptedError("下载已取消。")
            chunk = resp.read(READ_SIZE)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            counter.add(len(chunk))
        # http.client returns b"" on a dropped connection instead of raising;
        # a truncated .part must never be promoted to a loadable GGUF.
        if expected is not None and done != expected:
            raise OSError(f"Download was incomplete: got {done} bytes, expected {expected}.")
    tmp_path.replace(dest)


def parallel_chunked_download(
    ctx: AppContext,
    url: str,
    headers: dict[str, str],
    dest: pathlib.Path,
    completed_bytes: int,
    total_bytes: int,
    track: str,
    urlopen: UrlOpen,
    progress: Optional[SharedProgress] = None,
    filename: str = "",
    chunk_size: int = CHUNK_SIZE,
) -> int:
    """Download *url* to *dest* with parallel Range chunks.

    Falls back to a single stream when the server does not honour Range
    requests or the size is unknown. Returns the bytes written by this call
    (base offset included).
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    progress = progress or SharedProgress(ctx)
    progress.configure(track, filename or dest.name, completed_bytes, total_bytes)
    counter = _Counter(progress, track)

    supports_ranges, probed_total = probe_range_support(url, headers, urlopen)
    total = total_bytes or probed_total

    part_paths: list[pathlib.Path] = []
    try:
        if total <= 0 or not supports_ranges:
            _single_stream(ctx, url, headers, dest, counter, urlopen)
        else:
            chunk_count = min(
                MAX_WORKERS,
                max(1, total // chunk_size + (1 if total % chunk_size else 0)),
            )
            base = total // chunk_count
            spans = []
            start = 0
            for index in range(chunk_count):
                end = total - 1 if index == chunk_count - 1 else start + base - 1
                spans.append((start, end))
                start = end + 1
            part_paths = [
                dest.with_suffix(dest.suffix + f".part{index}")
                for index in range(len(spans))
            ]
            with ThreadPoolExecutor(max_workers=len(spans)) as pool:
                futures = [
                    pool.submit(
                        _download_chunk,
                        ctx,
                        url,
                        headers,
                        span_start,
                        span_end,
                        part_path,
                        counter,
                        urlopen,
                    )
                    for (span_start, span_end), part_path in zip(spans, part_paths)
                ]
                for future in futures:
                    future.result()

            assembled = dest.with_suffix(dest.suffix + ".assembling")
            with open(assembled, "wb") as out:
                for part_path in part_paths:
                    with open(part_path, "rb") as part_file:
                        while True:
                            buf = part_file.read(READ_SIZE)
                            if not buf:
                                break
                            out.write(buf)
            if assembled.stat().st_size != total:
                raise OSError(f"Assembled size mismatch for {dest.name}.")
            assembled.replace(dest)
        return counter.done()
    finally:
        for part_path in part_paths:
            try:
                if part_path.exists():
                    part_path.unlink()
            except OSError as exc:
                print(f"[chunked_download] failed to remove chunk file: {exc}", flush=True)
