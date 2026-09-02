"""Download the Piper voice model, once, into a persisted directory.

Piper needs two files on disk: an ONNX model (~60 MB) and its JSON config. They are not
baked into the image because doing so would make every rebuild download them again and
would tie the image to one voice.

Deliberately synchronous. This runs once at container start, before the event loop
exists, and there is nothing to overlap it with — an async version would only be async
in shape.

Idempotent and non-fatal by design. If the download fails — no network at boot, an
upstream outage — this exits 0 and the TTS provider reports the missing model when it is
first asked to speak. A voice that cannot be fetched is a degraded discussion, not a
reason for the API to refuse to start.

    python -m scripts.fetch_piper_voice
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx

from app.core.config import settings
from app.core.logging import configure_logging, get_logger

log = get_logger(__name__)

#: Voices are published per-language under this prefix, one directory per voice.
BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"


def voice_url(voice: str) -> str:
    """``en_US-lessac-medium`` → ``…/en/en_US/lessac/medium/en_US-lessac-medium``."""
    locale, rest = voice.split("-", 1)
    name, quality = rest.rsplit("-", 1)
    language = locale.split("_")[0]
    return f"{BASE}/{language}/{locale}/{name}/{quality}/{voice}"


def _download(client: httpx.Client, url: str, target: Path) -> None:
    tmp = target.with_name(target.name + ".part")
    with client.stream("GET", url, follow_redirects=True) as response:
        response.raise_for_status()
        total = int(response.headers.get("content-length", 0))
        written = 0
        with tmp.open("wb") as handle:
            for chunk in response.iter_bytes(chunk_size=1 << 20):
                handle.write(chunk)
                written += len(chunk)
        if total and written != total:
            raise OSError(f"truncated download: {written} of {total} bytes")
    # Renamed only once every byte is there, so an interrupted run cannot leave a
    # half-written model that looks present to the provider.
    tmp.replace(target)


def fetch() -> int:
    model = Path(settings.piper_model_path)
    # Piper's own convention: the config sits beside the model as "<model>.onnx.json",
    # so this is a plain append, not a suffix replacement.
    config = Path(f"{model}.json")

    if model.is_file() and config.is_file():
        log.info("piper.voice_present", path=str(model))
        return 0

    model.parent.mkdir(parents=True, exist_ok=True)
    base = voice_url(settings.piper_voice)
    log.info("piper.downloading", voice=settings.piper_voice, into=str(model.parent))

    timeout = httpx.Timeout(connect=10, read=120, write=30, pool=10)
    try:
        with httpx.Client(timeout=timeout) as client:
            if not config.is_file():
                _download(client, f"{base}.onnx.json", config)
            if not model.is_file():
                _download(client, f"{base}.onnx", model)
    except Exception as exc:  # reported, never fatal — see the module docstring
        log.warning("piper.download_failed", voice=settings.piper_voice, error=str(exc))
        return 0

    log.info("piper.voice_ready", path=str(model), bytes=model.stat().st_size)
    return 0


if __name__ == "__main__":
    configure_logging()
    sys.exit(fetch())
