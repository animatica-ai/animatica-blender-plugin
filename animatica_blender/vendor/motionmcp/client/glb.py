# SPDX-License-Identifier: Apache-2.0
"""Binary glTF (GLB) in, plain glTF JSON document out.

A server may answer ``/generate`` with ``model/gltf-binary`` instead of
``model/gltf+json``. :func:`glb_to_gltf` unpacks the container -- a 12-byte
header, a ``JSON`` chunk and an optional ``BIN`` chunk -- and returns the
same kind of document the JSON path returns: the buffer without a ``uri``
(glTF: buffer 0 is the ``BIN`` chunk) gets the chunk's bytes as a base64
``data:`` URI. The result is therefore JSON-serialisable (no ``bytes``
inside) and :mod:`motionmcp.client.gltf_parser` reads it unchanged. The
price is base64 of the ``BIN`` chunk, about 1.33x its size while converting.

Standard library only, like :mod:`motionmcp.client.http`.
"""

from __future__ import annotations

import base64
import json
import struct

__all__ = ["glb_to_gltf"]

_MAGIC = b"glTF"
_CHUNK_JSON = 0x4E4F534A
_CHUNK_BIN = 0x004E4942


def glb_to_gltf(data: bytes) -> dict:
    """Return the glTF JSON document packed in the GLB *data*.

    Raises ``ValueError`` on a bad magic, a version other than 2, a
    ``length`` that disagrees with ``len(data)``, a truncated chunk, a
    missing leading ``JSON`` chunk or a JSON chunk that is not an object.
    """
    if len(data) < 12:
        raise ValueError(f"GLB shorter than its 12-byte header ({len(data)} bytes)")
    magic, version, length = struct.unpack_from("<4sII", data, 0)
    if magic != _MAGIC:
        raise ValueError(f"not a GLB: magic {magic!r}")
    if version != 2:
        raise ValueError(f"unsupported GLB version {version}")
    if length != len(data):
        raise ValueError(f"GLB length field {length} != actual size {len(data)}")

    chunks = []
    offset = 12
    while offset < length:
        if offset + 8 > length:
            raise ValueError("truncated GLB chunk header")
        chunk_len, chunk_type = struct.unpack_from("<II", data, offset)
        start = offset + 8
        end = start + chunk_len
        if end > length:
            raise ValueError("GLB chunk runs past the end of the file")
        chunks.append((chunk_type, data[start:end]))
        offset = end

    if not chunks or chunks[0][0] != _CHUNK_JSON:
        raise ValueError("GLB does not start with a JSON chunk")
    doc = json.loads(chunks[0][1].decode("utf-8"))
    if not isinstance(doc, dict):
        raise ValueError("GLB JSON chunk is not an object")

    bin_chunk = None
    if len(chunks) > 1 and chunks[1][0] == _CHUNK_BIN:
        bin_chunk = chunks[1][1]
    buffers = doc.get("buffers")
    if bin_chunk is not None and isinstance(buffers, list) and buffers:
        first = buffers[0]
        if isinstance(first, dict) and "uri" not in first:
            byte_length = first.get("byteLength")
            if isinstance(byte_length, int) and 0 <= byte_length <= len(bin_chunk):
                bin_chunk = bin_chunk[:byte_length]  # drop the chunk's 4-byte padding
            first["uri"] = ("data:application/octet-stream;base64,"
                            + base64.b64encode(bin_chunk).decode("ascii"))
    return doc
