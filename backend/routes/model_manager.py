"""Routes for model management: info, delete (two-step), reveal folder."""

import sys
import urllib.parse

from backend.http import sanitize_error
from backend.services import lifecycle as lifecycle_service
from backend.services import model_manager


def get_model_info(request, response, ctx):
    rel = urllib.parse.unquote(request.params.get("path", ""))
    try:
        response.json(model_manager.model_info(ctx, rel))
    except FileNotFoundError as exc:
        response.error(str(exc), 404)
    except model_manager.ModelPathError as exc:
        response.error(str(exc), 400)
    except Exception as exc:
        print(f"[model_manager] info failed: {exc}", file=sys.stderr)
        response.error(sanitize_error(exc, 500), 500)


def post_model_stat(request, response, ctx):
    body = request.body or {}
    try:
        response.json(model_manager.stat_for_delete(ctx, body.get("path", "")))
    except FileNotFoundError as exc:
        response.error(str(exc), 404)
    except model_manager.ModelPathError as exc:
        response.error(str(exc), 400)
    except Exception as exc:
        print(f"[model_manager] stat failed: {exc}", file=sys.stderr)
        response.error(sanitize_error(exc, 500), 500)


def post_model_delete(request, response, ctx):
    body = request.body or {}
    try:
        result = model_manager.delete_model(ctx, body.get("path", ""))
        print(f"[model_manager] deleted: {result}", file=sys.stderr)
        response.json(result)
    except FileNotFoundError as exc:
        response.error(str(exc), 404)
    except model_manager.ModelPathError as exc:
        response.error(str(exc), 400)
    except OSError as exc:
        # Windows: file locked by a running llama-server lands here.
        print(f"[model_manager] delete failed: {exc}", file=sys.stderr)
        response.error(f"删除失败（文件可能正在被使用）: {exc}", 409)
    except Exception as exc:
        print(f"[model_manager] delete failed: {exc}", file=sys.stderr)
        response.error(sanitize_error(exc, 500), 500)


def post_model_reveal(request, response, ctx):
    body = request.body or {}
    try:
        folder = model_manager.reveal_model(ctx, body.get("path", ""))
        lifecycle_service.open_folder_in_file_manager(folder)
        response.json({"opened": True, "folder": str(folder)})
    except FileNotFoundError as exc:
        response.error(str(exc), 404)
    except model_manager.ModelPathError as exc:
        response.error(str(exc), 400)
    except Exception as exc:
        print(f"[model_manager] reveal failed: {exc}", file=sys.stderr)
        response.error(sanitize_error(exc, 500), 500)
