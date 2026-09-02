"""Pure-Python stand-ins for the ``audioop`` functions this project uses.

``audioop`` was removed from the standard library in Python 3.13. Only ``add``, ``mul``,
``ratecv`` and ``rms`` are needed, all for 16-bit mono, so a small linear implementation
is enough.
"""

from __future__ import annotations

import array
import math
from typing import Any

_MIN = -32768
_MAX = 32767


def rms(fragment: bytes, width: int) -> int:
    """Root-mean-square amplitude — the energy gate the Whisper adapter runs on."""
    if width != 2:  # pragma: no cover - the project only ever uses PCM16
        raise ValueError("only 16-bit samples are supported")
    samples = array.array("h")
    samples.frombytes(fragment)
    if not samples:
        return 0
    total = sum(value * value for value in samples)
    return int(math.sqrt(total / len(samples)))


def add(fragment1: bytes, fragment2: bytes, width: int) -> bytes:
    if width != 2:  # pragma: no cover - the project only ever uses PCM16
        raise ValueError("only 16-bit samples are supported")
    a = array.array("h")
    a.frombytes(fragment1)
    b = array.array("h")
    b.frombytes(fragment2)
    out = array.array("h", bytes(len(fragment1)))
    for i in range(min(len(a), len(b))):
        out[i] = max(_MIN, min(_MAX, a[i] + b[i]))
    return out.tobytes()


def mul(fragment: bytes, width: int, factor: float) -> bytes:
    if width != 2:  # pragma: no cover
        raise ValueError("only 16-bit samples are supported")
    samples = array.array("h")
    samples.frombytes(fragment)
    for i, value in enumerate(samples):
        samples[i] = max(_MIN, min(_MAX, int(value * factor)))
    return samples.tobytes()


def ratecv(
    fragment: bytes,
    width: int,
    nchannels: int,
    inrate: int,
    outrate: int,
    state: Any,
    weightA: int = 1,
    weightB: int = 0,
) -> tuple[bytes, Any]:
    if width != 2 or nchannels != 1:  # pragma: no cover
        raise ValueError("only 16-bit mono is supported")
    if inrate == outrate:
        return fragment, state

    source = array.array("h")
    source.frombytes(fragment)
    if not source:
        return b"", state

    ratio = outrate / inrate
    out_len = int(len(source) * ratio)
    out = array.array("h", bytes(out_len * 2))
    for i in range(out_len):
        position = i / ratio
        left = int(position)
        right = min(left + 1, len(source) - 1)
        frac = position - left
        out[i] = int(source[left] * (1 - frac) + source[right] * frac)
    return out.tobytes(), state
