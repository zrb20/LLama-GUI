"""Model manager: per-model info, delete, reveal-in-folder.

Complements routes/models.py (which lists launchable models): this module
adds management operations with path-safety checks — every model path must
resolve inside the active model root, deletion is two-step (stat first for
the confirmation size, then delete).
"""

import pathlib
import shutil

from backend.context import AppContext
from backend.services import model_dir
from backend.services.gguf_meta import GgufParseError, summarize_gguf


class ModelPathError(Exception):
    """The requested path is outside the active model root."""


def _resolve_model_path(ctx: AppContext, rel_path: str) -> pathlib.Path:
    """Resolve a model-root-relative path, refusing escapes.

    ``..`` segments, absolute paths, and symlinked escapes are all rejected:
    only paths that resolve back inside the model root are valid targets.
    """
    if not isinstance(rel_path, str) or not rel_path.strip():
        raise ModelPathError("缺少模型路径。")
    models_dir = model_dir.get_models_dir(ctx).resolve()
    candidate = (models_dir / rel_path).resolve()
    if candidate != models_dir and models_dir not in candidate.parents:
        raise ModelPathError("路径越出模型根目录，已拒绝。")
    return candidate


def model_exists(ctx: AppContext, rel_path: str) -> bool:
    try:
        return _resolve_model_path(ctx, rel_path).is_file()
    except (ModelPathError, ValueError):
        return False


def model_info(ctx: AppContext, rel_path: str) -> dict:
    """File facts + GGUF metadata for one model (relative path)."""
    path = _resolve_model_path(ctx, rel_path)
    if not path.is_file():
        raise FileNotFoundError(f"模型文件不存在: {rel_path}")
    size = path.stat().st_size
    info: dict = {
        "path": rel_path,
        "absolute_path": str(path),
        "size_bytes": size,
        "size_gb": round(size / (1024 ** 3), 2),
    }
    if path.suffix.lower() == ".gguf":
        try:
            info["gguf"] = summarize_gguf(path)
        except GgufParseError as exc:
            info["gguf_error"] = f"无法解析 GGUF 头: {exc}"
        except OSError as exc:
            info["gguf_error"] = f"读取失败: {exc}"
    return info


def stat_for_delete(ctx: AppContext, rel_path: str) -> dict:
    """Preview what a delete would remove: path, size, file count."""
    path = _resolve_model_path(ctx, rel_path)
    if not path.exists():
        raise FileNotFoundError(f"路径不存在: {rel_path}")

    if path.is_file():
        return {"path": rel_path, "files": 1, "size_bytes": path.stat().st_size}

    total = 0
    count = 0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
                count += 1
            except OSError:
                continue
    return {"path": rel_path, "files": count, "size_bytes": total}


def delete_model(ctx: AppContext, rel_path: str) -> dict:
    """Delete one model file, or a repo folder with all of its contents."""
    path = _resolve_model_path(ctx, rel_path)
    if not path.exists():
        raise FileNotFoundError(f"路径不存在: {rel_path}")
    # Safety floor: never delete the model root itself.
    if path == model_dir.get_models_dir(ctx).resolve():
        raise ModelPathError("不能删除模型根目录。")

    if path.is_file():
        size = path.stat().st_size
        path.unlink()
        return {"deleted": rel_path, "files": 1, "size_bytes": size}

    stat = stat_for_delete(ctx, rel_path)
    shutil.rmtree(path)
    return {"deleted": rel_path, "files": stat["files"], "size_bytes": stat["size_bytes"]}


def reveal_model(ctx: AppContext, rel_path: str) -> pathlib.Path:
    """Return the parent folder of a model for reveal-in-file-manager."""
    path = _resolve_model_path(ctx, rel_path)
    if not path.exists():
        raise FileNotFoundError(f"路径不存在: {rel_path}")
    return path.parent
