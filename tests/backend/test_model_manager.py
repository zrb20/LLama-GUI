"""Model manager tests: GGUF metadata parsing, path safety, delete flow."""

import json
import pathlib
import struct
import tempfile
import unittest
from unittest import mock

from backend.services import gguf_meta, model_manager
from tests.backend.test_services import make_service_context


def _tiny_gguf(metadata: dict) -> bytes:
    """Build a minimal valid GGUF v3 file with the given scalar metadata."""
    buf = b"GGUF" + struct.pack("<IQQ", 3, 0, len(metadata))
    for key, value in metadata.items():
        kb = key.encode("utf-8")
        buf += struct.pack("<Q", len(kb)) + kb
        if isinstance(value, bool):
            buf += struct.pack("<I", 7) + struct.pack("<B", 1 if value else 0)
        elif isinstance(value, int):
            buf += struct.pack("<I", 4) + struct.pack("<I", value)
        elif isinstance(value, float):
            buf += struct.pack("<I", 6) + struct.pack("<f", value)
        else:
            vb = str(value).encode("utf-8")
            buf += struct.pack("<I", 8) + struct.pack("<Q", len(vb)) + vb
    return buf


class GgufMetaTests(unittest.TestCase):
    def test_reads_scalar_metadata(self):
        data = _tiny_gguf({
            "general.architecture": "llama",
            "general.name": "TestModel Q4_K_M",
            "general.file_type": 16,
            "llama.context_length": 131072,
            "llama.block_count": 28,
        })
        with tempfile.TemporaryDirectory() as tmp:
            p = pathlib.Path(tmp) / "m.gguf"
            p.write_bytes(data)
            summary = gguf_meta.summarize_gguf(p)

        self.assertEqual(summary["architecture"], "llama")
        self.assertEqual(summary["quantization"], "Q4_K_M")
        self.assertEqual(summary["context_length"], 131072)
        self.assertEqual(summary["block_count"], 28)

    def test_rejects_non_gguf(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = pathlib.Path(tmp) / "m.gguf"
            p.write_bytes(b"NOTG" + b"\x00" * 64)
            with self.assertRaises(gguf_meta.GgufParseError):
                gguf_meta.read_gguf_metadata(p)


class ModelManagerPathSafetyTests(unittest.TestCase):
    def test_delete_refuses_path_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = make_service_context(tmp)
            outside = pathlib.Path(tmp) / "outside.gguf"
            outside.write_bytes(b"x")
            with self.assertRaises(model_manager.ModelPathError):
                model_manager.delete_model(ctx, "../outside.gguf")

    def test_delete_refuses_absolute_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = make_service_context(tmp)
            with self.assertRaises(model_manager.ModelPathError):
                model_manager.delete_model(ctx, "C:/Windows/system32")

    def test_delete_file_reports_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = make_service_context(tmp)
            model = ctx.paths.models / "repo" / "model.gguf"
            model.parent.mkdir(parents=True)
            model.write_bytes(b"0123456789")

            stat = model_manager.stat_for_delete(ctx, "repo/model.gguf")
            self.assertEqual(stat["files"], 1)
            self.assertEqual(stat["size_bytes"], 10)

            result = model_manager.delete_model(ctx, "repo/model.gguf")
            self.assertEqual(result["deleted"], "repo/model.gguf")
            self.assertFalse(model.exists())

    def test_delete_folder_removes_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = make_service_context(tmp)
            repo = ctx.paths.models / "repo"
            repo.mkdir(parents=True)
            (repo / "a.gguf").write_bytes(b"A" * 5)
            (repo / "b.gguf").write_bytes(b"B" * 7)

            result = model_manager.delete_model(ctx, "repo")
            self.assertEqual(result["files"], 2)
            self.assertEqual(result["size_bytes"], 12)
            self.assertFalse(repo.exists())

    def test_delete_refuses_model_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = make_service_context(tmp)
            with self.assertRaises(model_manager.ModelPathError):
                model_manager.delete_model(ctx, ".")

    def test_info_parses_gguf(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = make_service_context(tmp)
            model = ctx.paths.models / "m.gguf"
            model.parent.mkdir(parents=True)
            model.write_bytes(_tiny_gguf({
                "general.architecture": "llama",
                "general.file_type": 16,
            }))
            info = model_manager.model_info(ctx, "m.gguf")
            self.assertEqual(info["size_bytes"], len(model.read_bytes()))
            self.assertEqual(info["gguf"]["architecture"], "llama")
            self.assertEqual(info["gguf"]["quantization"], "Q4_K_M")

    def test_reveal_returns_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = make_service_context(tmp)
            model = ctx.paths.models / "repo" / "m.gguf"
            model.parent.mkdir(parents=True)
            model.write_bytes(b"x")
            folder = model_manager.reveal_model(ctx, "repo/m.gguf")
            self.assertEqual(folder, ctx.paths.models / "repo")


if __name__ == "__main__":
    unittest.main()
