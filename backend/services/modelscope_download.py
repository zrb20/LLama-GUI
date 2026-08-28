"""ModelScope (魔搭) model discovery and multi-threaded chunked downloads.

Mirrors :mod:`backend.services.hf_download` but talks to the ModelScope REST
API directly (no vendor SDK) and fetches each file with parallel HTTP Range
chunks. Both services share the same ``ctx.state.model_download`` state, the
same lock/cancel contract, and the same frontend progress poller — only one
download can run at a time regardless of source.
"""

import json
import pathlib
import threading
import urllib.error
import urllib.request
from typing import Any, Callable

from backend.context import AppContext
from backend.http import sanitize_error
from backend.services import model_dir
from backend.services.http_chunks import (
    SharedProgress,
    parallel_chunked_download,
    probe_range_support,
)
from backend.services.hf_download import (
    get_model_download_snapshot,
    is_mmproj_filename,
    remove_partial_downloads,
    reset_model_download_state,
    set_model_download_state,
    slugify_repo_id,
    validate_hf_filename,
    validate_hf_repo_id,
)

UrlOpen = Callable[..., Any]

MS_API_BASE = "https://www.modelscope.cn"
MS_REPO_FILES_API = MS_API_BASE + "/api/v1/models/{repo_id}/repo/files?Recursive=true"
MS_FILE_URL = MS_API_BASE + "/models/{repo_id}/resolve/master/{path}"
USER_AGENT = "Llama-GUI"


def get_ms_model_files(repo_id: str, urlopen: UrlOpen = urllib.request.urlopen) -> dict[str, Any]:
    """List the repo's GGUF files: {repo_id, revision, models, mmproj}."""
    url = MS_REPO_FILES_API.format(repo_id=repo_id)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    files = (payload.get("Data") or {}).get("Files") or []
    items = []
    for entry in files:
        if entry.get("Type") != "blob":
            continue
        name = str(entry.get("Path") or "")
        if not name.lower().endswith(".gguf"):
            continue
        try:
            size = int(entry.get("Size") or 0)
        except (TypeError, ValueError):
            size = 0
        items.append({"name": name, "size": size, "size_mb": round(size / 1048576, 2)})
    items.sort(key=lambda item: item["name"].lower())
    main_files = [item for item in items if not is_mmproj_filename(item["name"])]
    mmproj_files = [item for item in items if is_mmproj_filename(item["name"])]
    return {"repo_id": repo_id, "revision": "master", "models": main_files, "mmproj": mmproj_files}


def build_ms_download_url(repo_id: str, filename: str) -> str:
    return MS_FILE_URL.format(repo_id=repo_id, path=filename)


def annotate_exists(ctx: AppContext, files: dict[str, Any]) -> dict[str, Any]:
    """Add "exists" to each listed file based on the active model root."""
    try:
        models_dir = model_dir.get_models_dir(ctx)
    except Exception:  # noqa: BLE001 - listing stays usable without a root
        return files
    for group in ("models", "mmproj"):
        for item in files.get(group) or []:
            basename = pathlib.PurePosixPath(item["name"]).name
            folder = slugify_repo_id(files.get("repo_id", "repo"))
            item["exists"] = (models_dir / folder / basename).is_file()
    return files


def get_ms_file_size(repo_id: str, filename: str, urlopen: UrlOpen = urllib.request.urlopen) -> int:
    """Declared file size via a 1-byte Range probe, 0 when the server won't say.

    ModelScope's resolve endpoint answers HEAD without Content-Length (the 302
    hop has only a tiny redirect body), but a GET with ``Range: bytes=0-0``
    comes back 206 with ``Content-Range: bytes 0-0/<total>`` from the CDN.
    """
    supported, total = probe_range_support(
        build_ms_download_url(repo_id, filename), {"User-Agent": USER_AGENT}, urlopen
    )
    if total:
        return total
    try:
        request = urllib.request.Request(
            build_ms_download_url(repo_id, filename), headers={"User-Agent": USER_AGENT}
        )
        with urlopen(request, timeout=30) as resp:
            raw = resp.headers.get("Content-Length")
            return int(raw) if raw else 0
    except (OSError, ValueError, urllib.error.URLError) as exc:
        print(f"[ms_download] failed to read file size for {repo_id}/{filename}: {exc}", flush=True)
        return 0


def download_ms_file(
    ctx: AppContext,
    repo_id: str,
    filename: str,
    dest: pathlib.Path,
    completed_bytes: int,
    total_bytes: int,
    urlopen: UrlOpen = urllib.request.urlopen,
    track: str = "",
    progress: SharedProgress | None = None,
) -> int:
    """Download one file with parallel Range chunks; returns bytes written.

    Thin wrapper over the shared engine with ModelScope URL/headers. Chunk
    temp files are removed in ``finally`` per the AGENTS.md contract.
    """
    return parallel_chunked_download(
        ctx,
        build_ms_download_url(repo_id, filename),
        {"User-Agent": USER_AGENT},
        dest,
        completed_bytes,
        total_bytes,
        track or filename,
        urlopen,
        progress=progress,
        filename=filename,
    )


def start_ms_model_download(
    ctx: AppContext,
    repo_id: Any,
    model_file: Any,
    mmproj_file: Any,
    overwrite: bool = False,
    urlopen: UrlOpen = urllib.request.urlopen,
) -> dict[str, Any]:
    """Validate inputs and spawn the background download worker (HF twin)."""
    repo_id = validate_hf_repo_id(repo_id)
    model_file = validate_hf_filename(model_file)
    mmproj_file = validate_hf_filename(mmproj_file) if mmproj_file else ""

    if is_mmproj_filename(model_file):
        raise ValueError("Choose a main model file, not an mmproj file.")
    if mmproj_file and not is_mmproj_filename(mmproj_file):
        raise ValueError("Choose an mmproj/projector file for the companion mmproj download.")

    with ctx.state.model_download_lock:
        if ctx.state.model_download_in_progress:
            raise RuntimeError("已有模型下载正在进行。")
        models_dir = model_dir.get_models_dir(ctx)
        repo_folder = slugify_repo_id(repo_id)
        model_basename = pathlib.PurePosixPath(model_file).name
        model_name = f"{repo_folder}/{model_basename}"
        model_dest = models_dir / repo_folder / model_basename
        mmproj_dest = None
        if mmproj_file:
            mmproj_dest = model_dest.parent / pathlib.PurePosixPath(mmproj_file).name

        existing = []
        if model_dest.exists():
            existing.append(model_name)
        if mmproj_dest and mmproj_dest.exists():
            existing.append(f"{repo_folder}/{mmproj_dest.name}")
        if existing and not overwrite:
            raise FileExistsError(f"已存在：{', '.join(existing)}")

        ctx.state.model_download_in_progress = True
        ctx.state.model_download_cancel.clear()
        reset_model_download_state(
            ctx, status="starting", message="正在准备魔搭下载…"
        )

    def _worker() -> None:
        destinations = [model_dest]
        if mmproj_dest:
            destinations.append(mmproj_dest)
        try:
            model_dest.parent.mkdir(parents=True, exist_ok=True)

            # Probe both sizes up front, then download model and mmproj in
            # parallel so the total progress reflects both streams at once.
            model_total = get_ms_file_size(repo_id, model_file, urlopen)
            mmproj_total = (
                get_ms_file_size(repo_id, mmproj_file, urlopen) if mmproj_file else 0
            )
            total = model_total + mmproj_total
            reset_model_download_state(
                ctx,
                status="downloading",
                message=f"正在下载 {model_name}…",
                total=total,
                downloaded=0,
            )
            mmproj_path = ""
            if mmproj_file and mmproj_dest:
                # Download model and mmproj concurrently; each stream reports
                # its own track (model/mmproj) so the UI can show two bars.
                mmproj_results: list[int] = []

                def _run_mmproj() -> None:
                    mmproj_results.append(
                        download_ms_file(
                            ctx,
                            repo_id,
                            mmproj_file,
                            mmproj_dest,
                            0,
                            mmproj_total,
                            urlopen,
                            track="mmproj",
                        )
                    )

                mmproj_total = get_ms_file_size(repo_id, mmproj_file, urlopen)
                mmproj_thread = threading.Thread(target=_run_mmproj, daemon=True)
                mmproj_thread.start()
                model_bytes = download_ms_file(
                    ctx,
                    repo_id,
                    model_file,
                    model_dest,
                    0,
                    model_total,
                    urlopen,
                    track="model",
                )
                mmproj_thread.join()
                completed = model_bytes + (mmproj_results[0] if mmproj_results else 0)
                mmproj_path = str(mmproj_dest)
            else:
                completed = download_ms_file(
                    ctx, repo_id, model_file, model_dest, 0, total, urlopen
                )
            set_model_download_state(
                ctx,
                status="done",
                message=f"已下载 {model_name}。",
                downloaded=total or completed,
                total=total or completed,
                current_file="",
                model_name=model_name,
                model_path=str(model_dest),
                mmproj_path=mmproj_path,
            )
        except InterruptedError as exc:
            remove_partial_downloads(destinations)
            set_model_download_state(ctx, status="cancelled", message=str(exc), current_file="")
        except Exception as exc:
            remove_partial_downloads(destinations)
            set_model_download_state(
                ctx,
                status="error",
                message=sanitize_error(exc, 500),
                current_file="",
            )
        finally:
            with ctx.state.model_download_lock:
                ctx.state.model_download_in_progress = False
                ctx.state.model_download_cancel.clear()

    threading.Thread(target=_worker, daemon=True).start()
    return get_model_download_snapshot(ctx)
