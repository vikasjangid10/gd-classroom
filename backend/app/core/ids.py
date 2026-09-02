"""Time-sortable identifiers.

UUIDv7 keeps primary keys ordered by creation time, which keeps B-tree inserts at the
right-hand edge of the index instead of scattering them like UUIDv4 does. Python 3.11
has no ``uuid.uuid7``, so this is the RFC 9562 layout implemented directly.
"""

from __future__ import annotations

import os
import time
import uuid


def uuid7() -> uuid.UUID:
    ms = int(time.time() * 1000) & 0xFFFFFFFFFFFF
    rand = int.from_bytes(os.urandom(10), "big")

    value = ms << 80
    value |= 0x7 << 76  # version 7
    value |= ((rand >> 68) & 0xFFF) << 64  # rand_a
    value |= 0b10 << 62  # RFC 4122 variant
    value |= rand & 0x3FFFFFFFFFFFFFFF  # rand_b
    return uuid.UUID(int=value)


def uuid7_str() -> str:
    return str(uuid7())
