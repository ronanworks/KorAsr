"""Download the speaker-segmentation model used for turn-level diarization and verify checksums.

Source: official k2-fsa/sherpa-onnx GitHub release "speaker-segmentation-models":
pyannote segmentation-3.0 exported to ONNX (MIT licence, ~6 MB inside a 7 MB .tar.bz2).
Only model.onnx is kept (the int8 variant is not faster on CPU here). Run once from the project root:
    python scripts/download_diarization_model.py
The CAM++ voiceprint model comes from scripts/download_speaker_model.py (needed as well).
"""
import hashlib
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ARCHIVE = "sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
URL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/{ARCHIVE}"
ARCHIVE_SHA256 = "24615ee884c897d9d2ba09bb4d30da6bb1b15e685065962db5b02e76e4996488"
MEMBER = "sherpa-onnx-pyannote-segmentation-3-0/model.onnx"
MODEL_SHA256 = "220ad67ca923bef2fa91f2390c786097bf305bceb5e261d4af67b38e938e1079"
DEST = Path(__file__).resolve().parents[1] / "models" / "speaker" / "sherpa-onnx-pyannote-segmentation-3-0.onnx"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    if DEST.exists() and sha256(DEST) == MODEL_SHA256:
        print(f"already present: {DEST}")
        return 0
    DEST.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / ARCHIVE
        print(f"downloading {URL}")
        urllib.request.urlretrieve(URL, archive)
        actual = sha256(archive)
        if actual != ARCHIVE_SHA256:
            print(f"archive checksum mismatch: {actual}", file=sys.stderr)
            return 1
        partial = DEST.with_suffix(".part")
        with tarfile.open(archive, "r:bz2") as tar:
            src = tar.extractfile(MEMBER)
            if src is None:
                print(f"{MEMBER} missing from archive", file=sys.stderr)
                return 1
            partial.write_bytes(src.read())
    actual = sha256(partial)
    if actual != MODEL_SHA256:
        partial.unlink(missing_ok=True)
        print(f"model checksum mismatch: {actual}", file=sys.stderr)
        return 1
    partial.replace(DEST)
    print(f"saved {DEST} ({DEST.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
