"""Minimal GGUF metadata reader (pure stdlib, header only).

Reads the GGUF v2/v3 header and key/value metadata of a model file without
loading tensor data — a few KB per file even for 26GB models. Used by the
model manager to show architecture, parameter count, quantization, context
length, and other facts the launch UI otherwise cannot surface.
"""

import pathlib
import struct
from typing import Any

GGUF_MAGIC = b"GGUF"

# GGUF metadata value types (from ggml; only these appear in real models)
_TYPES = {
    0: ("u8", "<B", 1),
    1: ("i8", "<b", 1),
    2: ("u16", "<H", 2),
    3: ("i16", "<h", 2),
    4: ("u32", "<I", 4),
    5: ("i32", "<i", 4),
    6: ("f32", "<f", 4),
    7: ("bool", "<B", 1),
    8: ("str", None, None),  # u64 length + utf-8 bytes
    9: ("arr", None, None),  # type + u64 count + items
    10: ("u64", "<Q", 8),
    11: ("i64", "<q", 8),
    12: ("f64", "<d", 8),
}

# Keys worth surfacing in the manager UI (key → friendly label handled in the
# route; here we just decide what to keep from potentially hundreds of keys).
INTERESTING_PREFIXES = (
    "general.",
    "llama.context_length",
    "llama.embedding_length",
    "llama.block_count",
    "llama.attention.head_count",
    "llama.expert_count",
    "llama.expert_used_count",
    "qwen3.context_length",
    "qwen3.expert_count",
    "qwen3.expert_used_count",
)


class GgufParseError(Exception):
    pass


def _read_str(fh) -> str:
    (length,) = struct.unpack("<Q", fh.read(8))
    data = fh.read(length)
    if len(data) != length:
        raise GgufParseError("truncated string")
    return data.decode("utf-8", errors="replace")


def _read_value(fh, vtype: int) -> Any:
    if vtype not in _TYPES:
        raise GgufParseError(f"unknown value type {vtype}")
    name, fmt, size = _TYPES[vtype]
    if name == "str":
        return _read_str(fh)
    if name == "arr":
        (item_type,) = struct.unpack("<I", fh.read(4))
        (count,) = struct.unpack("<Q", fh.read(8))
        # Arrays we keep only in small form; big tensor lists are skipped by
        # the caller via key filtering, but we must still consume the bytes.
        if count > 64:
            raise GgufParseError("array too large to parse in header scan")
        return [_read_value(fh, item_type) for _ in range(count)]
    (value,) = struct.unpack(fmt, fh.read(size))
    if name == "bool":
        return bool(value)
    return value


def read_gguf_metadata(path: pathlib.Path) -> dict[str, Any]:
    """Return the GGUF header metadata as a plain dict, or raise GgufParseError.

    Only scalar/small-array keys are returned; large arrays (tokenizer vocab)
    are consumed but discarded to keep the payload small.
    """
    with open(path, "rb") as fh:
        magic = fh.read(4)
        if magic != GGUF_MAGIC:
            raise GgufParseError("not a GGUF file")
        # GGUF header: version u32, tensor_count u64, kv_count u64.
        version, tensor_count, kv_count = struct.unpack("<IQQ", fh.read(20))
        if version not in (2, 3):
            raise GgufParseError(f"unsupported GGUF version {version}")

        meta: dict[str, Any] = {}
        for _ in range(kv_count):
            key = _read_str(fh)
            (vtype,) = struct.unpack("<I", fh.read(4))
            if vtype == 9:  # array: check inner type size heuristically
                # consume without storing unless small and interesting
                (item_type,) = struct.unpack("<I", fh.read(4))
                (count,) = struct.unpack("<Q", fh.read(8))
                if key.startswith(INTERESTING_PREFIXES) and count <= 64:
                    meta[key] = [_read_value(fh, item_type) for _ in range(count)]
                else:
                    _name, _fmt, size = _TYPES.get(item_type, (None, None, None))
                    if size is None:
                        # nested str/arr inside arrays: parse and discard
                        for _ in range(count):
                            _read_value(fh, item_type)
                    else:
                        fh.seek(size * count, 1)
            else:
                value = _read_value(fh, vtype)
                if key.startswith(INTERESTING_PREFIXES):
                    meta[key] = value
        # Tensor count is useful for display too.
        meta["_tensor_count"] = tensor_count
        meta["_gguf_version"] = version
        return meta


def summarize_gguf(path: pathlib.Path) -> dict[str, Any]:
    """Human-friendly summary of a GGUF file for the model manager UI."""
    meta = read_gguf_metadata(path)

    def pick(*keys, default=None):
        for key in keys:
            if key in meta:
                return meta[key]
        return default

    arch = pick("general.architecture", default="")
    quant = pick("general.file_type")
    quant_map = {
        0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 7: "Q8_0", 8: "Q5_0",
        9: "Q5_1", 10: "Q2_K", 11: "Q2_K_S", 12: "Q3_K_S", 13: "Q3_K_M",
        14: "Q3_K_L", 15: "Q4_K_S", 16: "Q4_K_M", 17: "Q5_K_S", 18: "Q5_K_M",
        19: "Q6_K", 20: "IQ2_XXS", 21: "IQ2_XS", 22: "Q2_K", 23: "IQ3_XS",
        24: "IQ3_XXS", 25: "IQ1_S", 26: "IQ4_NL", 27: "IQ3_S", 28: "IQ3_M",
        29: "IQ2_S", 30: "IQ2_M", 31: "IQ4_XS", 32: "IQ1_M", 33: "BF16",
        36: "TQ1_0", 37: "TQ2_0",
    }
    quant_name = quant_map.get(quant) if isinstance(quant, int) else None
    if not quant_name:
        # Fall back to the name embedded in general.name / file name
        name = str(pick("general.name", default=path.stem))
        for tag in ("IQ4_XS", "IQ4_NL", "UD-IQ2_XXS", "Q8_0", "Q6_K", "Q5_K_M",
                    "Q4_K_M", "Q4_K_S", "Q4_K", "Q5_K", "Q3_K_M", "Q2_K", "BF16",
                    "F16", "F32", "Q8", "Q6", "Q5", "Q4", "Q3", "Q2"):
            if tag.lower() in name.lower():
                quant_name = tag
                break

    params = pick("general.size_label")
    return {
        "architecture": arch or None,
        "quantization": quant_name,
        "size_label": params,
        "context_length": pick(
            f"{arch}.context_length", "llama.context_length", "qwen3.context_length",
            default=None,
        ) if arch else pick("llama.context_length", "qwen3.context_length", default=None),
        "block_count": pick(f"{arch}.block_count", "llama.block_count", default=None) if arch else None,
        "embedding_length": pick(f"{arch}.embedding_length", "llama.embedding_length", default=None) if arch else None,
        "attention_heads": pick(f"{arch}.attention.head_count", "llama.attention.head_count", default=None) if arch else None,
        "expert_count": pick(f"{arch}.expert_count", "llama.expert_count", default=None) if arch else None,
        "expert_used": pick(f"{arch}.expert_used_count", "llama.expert_used_count", default=None) if arch else None,
        "model_name": pick("general.name", default=None),
        "tensor_count": meta.get("_tensor_count"),
        "gguf_version": meta.get("_gguf_version"),
    }
