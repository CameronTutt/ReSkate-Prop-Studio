from __future__ import annotations

from array import array
import json
import math
from pathlib import Path
import struct
import sys

try:
    import numpy as np
except ImportError:
    np = None


_ARRAY_BUFFER = 34962
_ELEMENT_ARRAY_BUFFER = 34963
_FLOAT = 5126
_UNSIGNED_SHORT = 5123
_UNSIGNED_INT = 5125
_TRIANGLES = 4


def _aligned(payload: bytes, fill: bytes = b"\x00") -> bytes:
    return payload + fill * (-len(payload) % 4)


def _finite(value, label: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} contains a non-finite component")
    return number


def _first_extreme(column, value):
    # A running Python min/max keeps the first of equal values, so a zero bound
    # takes the sign of the first zero in the stream.
    if value == 0.0:
        return float(column[int(np.argmax(column == 0.0))])
    return float(value)


def _vector_bytes_numpy(values, width: int, label: str, transform,
                        include_bounds):
    rows = np.asarray(values, dtype=np.float64)
    if rows.ndim != 2:
        return None
    if rows.shape[0] == 0:
        raise ValueError(f"{label} is empty")
    if rows.shape[1] < width:
        raise ValueError(f"{label} row 0 has fewer than {width} components")
    rows = rows[:, :width]
    if not np.isfinite(rows).all():
        raise ValueError(f"{label} contains a non-finite component")
    if transform is _game_vector:
        rows = np.stack((rows[:, 0], rows[:, 2], -rows[:, 1]), axis=1)
    elif transform is _gltf_uv:
        rows = np.stack((rows[:, 0], 1.0 - rows[:, 1]), axis=1)
    elif transform is not None:
        return None
    packed = np.ascontiguousarray(rows.astype(np.float32))
    minimum = [math.inf] * width
    maximum = [-math.inf] * width
    if include_bounds:
        low = packed.min(axis=0)
        high = packed.max(axis=0)
        minimum = [_first_extreme(packed[:, index], low[index]) for index in range(width)]
        maximum = [_first_extreme(packed[:, index], high[index]) for index in range(width)]
    if sys.byteorder != "little":
        packed = packed.byteswap()
    return packed.tobytes(), int(packed.shape[0]), minimum, maximum


def _vector_bytes(values, width: int, label: str, transform=None,
                  include_bounds=False):
    if np is not None:
        try:
            result = _vector_bytes_numpy(values, width, label, transform,
                                         include_bounds)
        except (TypeError, ValueError) as error:
            if isinstance(error, ValueError) and str(error).startswith(label):
                raise
            result = None
        if result is not None:
            return result
    packed = array("f")
    minimum = [math.inf] * width
    maximum = [-math.inf] * width
    count = 0
    for row in values:
        if len(row) < width:
            raise ValueError(f"{label} row {count} has fewer than {width} components")
        vector = tuple(_finite(row[index], label) for index in range(width))
        if transform is not None:
            vector = transform(vector)
        start = len(packed)
        packed.extend(vector)
        if include_bounds:


            for index in range(width):
                value = float(packed[start + index])
                minimum[index] = min(minimum[index], value)
                maximum[index] = max(maximum[index], value)
        count += 1
    if count == 0:
        raise ValueError(f"{label} is empty")
    if sys.byteorder != "little":
        packed.byteswap()
    return packed.tobytes(), count, minimum, maximum


def _index_bytes(values, vertex_count: int):
    if np is not None:
        indices = np.asarray(values, dtype=np.int64).ravel()
        if not indices.size or indices.size % 3:
            raise ValueError("triangle indices must contain one or more complete triangles")
        low, high = int(indices.min()), int(indices.max())
        if low < 0 or high >= vertex_count:
            raise ValueError(
                f"triangle index range {low}..{high} is outside {vertex_count} vertices")
        if high <= 0xFFFF:
            packed = indices.astype("<u2")
            component_type = _UNSIGNED_SHORT
        else:
            packed = indices.astype("<u4")
            component_type = _UNSIGNED_INT
        return packed.tobytes(), int(indices.size), component_type, low, high
    indices = tuple(int(value) for value in values)
    if not indices or len(indices) % 3:
        raise ValueError("triangle indices must contain one or more complete triangles")
    low, high = min(indices), max(indices)
    if low < 0 or high >= vertex_count:
        raise ValueError(
            f"triangle index range {low}..{high} is outside {vertex_count} vertices")
    if high <= 0xFFFF:
        packed = array("H", indices)
        component_type = _UNSIGNED_SHORT
    else:
        packed = array("I", indices)
        component_type = _UNSIGNED_INT
    if sys.byteorder != "little":
        packed.byteswap()
    return packed.tobytes(), len(indices), component_type, low, high


def _game_vector(vector):
    return vector[0], vector[2], -vector[1]


def _gltf_uv(vector):
    return vector[0], 1.0 - vector[1]


def encode_blender_mesh(positions, *, normals=None, uvs=None, indices=None,
                        name="mesh") -> bytes:


    position_bytes, vertex_count, position_min, position_max = _vector_bytes(
        positions, 3, "positions", _game_vector, include_bounds=True)
    if indices is None and vertex_count % 3:
        raise ValueError(
            f"non-indexed render stream has {vertex_count} vertices, not whole triangles")

    streams = []
    buffer_views = []
    accessors = []
    binary = bytearray()

    def add_stream(payload, *, target, component_type, count, kind,
                   minimum=None, maximum=None):
        offset = len(binary)
        binary.extend(payload)
        binary.extend(b"\x00" * (-len(binary) % 4))
        view_index = len(buffer_views)
        buffer_views.append({
            "buffer": 0,
            "byteOffset": offset,
            "byteLength": len(payload),
            "target": target,
        })
        accessor = {
            "bufferView": view_index,
            "byteOffset": 0,
            "componentType": component_type,
            "count": count,
            "type": kind,
        }
        if minimum is not None:
            accessor["min"] = minimum
        if maximum is not None:
            accessor["max"] = maximum
        accessors.append(accessor)
        streams.append(len(accessors) - 1)
        return streams[-1]

    position_accessor = add_stream(
        position_bytes, target=_ARRAY_BUFFER, component_type=_FLOAT,
        count=vertex_count, kind="VEC3", minimum=position_min,
        maximum=position_max)
    attributes = {"POSITION": position_accessor}

    if normals is not None:
        normal_bytes, normal_count, _minimum, _maximum = _vector_bytes(
            normals, 3, "normals", _game_vector)
        if normal_count != vertex_count:
            raise ValueError(
                f"normal count {normal_count} does not match {vertex_count} positions")
        attributes["NORMAL"] = add_stream(
            normal_bytes, target=_ARRAY_BUFFER, component_type=_FLOAT,
            count=normal_count, kind="VEC3")

    if uvs is not None:
        uv_bytes, uv_count, _minimum, _maximum = _vector_bytes(
            uvs, 2, "UVs", _gltf_uv)
        if uv_count != vertex_count:
            raise ValueError(
                f"UV count {uv_count} does not match {vertex_count} positions")
        attributes["TEXCOORD_0"] = add_stream(
            uv_bytes, target=_ARRAY_BUFFER, component_type=_FLOAT,
            count=uv_count, kind="VEC2")
        if len(uvs) and len(uvs[0]) > 2:
            weights = np.asarray(uvs)[:, 2:3] if np is not None else [row[2:3] for row in uvs]
            weight_bytes, count, _minimum, _maximum = _vector_bytes(weights, 1, "blend weights")
            attributes["_RESKATE_BLEND"] = add_stream(
                weight_bytes, target=_ARRAY_BUFFER, component_type=_FLOAT,
                count=count, kind="SCALAR")

    primitive = {"attributes": attributes, "mode": _TRIANGLES}
    if indices is not None:
        index_bytes, index_count, component_type, low, high = _index_bytes(
            indices, vertex_count)
        primitive["indices"] = add_stream(
            index_bytes, target=_ELEMENT_ARRAY_BUFFER,
            component_type=component_type, count=index_count, kind="SCALAR",
            minimum=[low], maximum=[high])

    document = {
        "asset": {"version": "2.0", "generator": "ReSkate normalized GLB writer"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": str(name)}],
        "meshes": [{"name": str(name), "primitives": [primitive]}],
        "buffers": [{"byteLength": len(binary)}],
        "bufferViews": buffer_views,
        "accessors": accessors,
    }
    json_chunk = _aligned(
        json.dumps(document, ensure_ascii=True, separators=(",", ":"),
                   sort_keys=True).encode("utf-8"), b" ")
    binary_chunk = bytes(binary)
    total = 12 + 8 + len(json_chunk) + 8 + len(binary_chunk)
    return (b"glTF" + struct.pack("<II", 2, total)
            + struct.pack("<II", len(json_chunk), 0x4E4F534A) + json_chunk
            + struct.pack("<II", len(binary_chunk), 0x004E4942) + binary_chunk)


def write_blender_mesh(path, positions, *, normals=None, uvs=None,
                       indices=None, name="mesh") -> None:
    payload = encode_blender_mesh(
        positions, normals=normals, uvs=uvs, indices=indices, name=name)
    Path(path).write_bytes(payload)
