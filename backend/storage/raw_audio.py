"""Paths and bounded readers for archival classroom recordings.

The browser sends mono 16 kHz float32 little-endian PCM.  The original bytes
are retained unchanged; WAV downloads convert one block at a time to 16-bit
PCM for broad player compatibility.
"""

from pathlib import Path
import shutil
from uuid import UUID

import numpy as np

from backend.config import settings


RAW_AUDIO_DIR = Path(settings.raw_audio_dir)
SAMPLE_RATE = 16_000
CHANNELS = 1
RAW_SAMPLE_BYTES = 4
WAV_SAMPLE_BYTES = 2
READ_CHUNK_BYTES = SAMPLE_RATE * RAW_SAMPLE_BYTES  # one second per chunk
MAX_WAV_DATA_BYTES = 0xFFFFFFFF - 36
LOW_DISK_WARNING_BYTES = 5 * 1024**3


def recording_path(session_id: str) -> Path:
    """Build a path only from a canonical UUID, never arbitrary URL text."""
    try:
        if str(UUID(session_id)) != session_id:
            raise ValueError("non-canonical UUID")
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid session ID") from exc
    return RAW_AUDIO_DIR / f"{session_id}.pcm"


def recording_duration_seconds(size_bytes: int) -> float:
    return size_bytes / (SAMPLE_RATE * CHANNELS * RAW_SAMPLE_BYTES)


def storage_free_bytes() -> int | None:
    """Report free space on the recording volume, even before its folder exists."""
    path = RAW_AUDIO_DIR.resolve()
    while not path.exists() and path != path.parent:
        path = path.parent
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def wav_data_size(raw_size_bytes: int) -> int:
    if raw_size_bytes % RAW_SAMPLE_BYTES:
        raise ValueError("Recording contains an incomplete float32 sample")
    data_size = raw_size_bytes // RAW_SAMPLE_BYTES * WAV_SAMPLE_BYTES
    if data_size > MAX_WAV_DATA_BYTES:
        raise OverflowError("Recording exceeds the WAV RIFF size limit")
    return data_size


def wav_header(data_size: int) -> bytes:
    """44-byte PCM16 WAVE header; the caller supplies the snapshot size."""
    import struct

    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + data_size, b"WAVE", b"fmt ", 16,
        1, CHANNELS, SAMPLE_RATE,
        SAMPLE_RATE * CHANNELS * WAV_SAMPLE_BYTES,
        CHANNELS * WAV_SAMPLE_BYTES, 16, b"data", data_size,
    )


def iter_pcm(path: Path, size_bytes: int):
    """Yield a fixed-size snapshot without ever reading the entire recording."""
    remaining = size_bytes
    with path.open("rb") as source:
        while remaining:
            chunk = source.read(min(READ_CHUNK_BYTES, remaining))
            if not chunk:
                raise OSError("Recording was truncated during download")
            remaining -= len(chunk)
            yield chunk


def iter_wav(path: Path, size_bytes: int):
    """Stream converted PCM16 WAVE audio in one-second blocks."""
    data_size = wav_data_size(size_bytes)
    yield wav_header(data_size)
    for raw in iter_pcm(path, size_bytes):
        samples = np.frombuffer(raw, dtype="<f4")
        safe = np.nan_to_num(samples, nan=0.0, posinf=1.0, neginf=-1.0)
        pcm16 = np.rint(np.clip(safe, -1.0, 1.0) * 32767).astype("<i2")
        yield pcm16.tobytes()
