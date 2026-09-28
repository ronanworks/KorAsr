"""Download the speaker-embedding model used for diarization and verify its checksum.

Source: official k2-fsa/sherpa-onnx GitHub release (3D-Speaker CAM++, zh+en,
192-dim, ~28 MB). Run once from the project root:
    python scripts/download_speaker_model.py
"""
import hashlib
import sys
import urllib.request
from pathlib import Path

NAME = "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx"
URL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/{NAME}"
SHA256 = "aa3cfc16963a10586a9393f5035d6d6b57e98d358b347f80c2a30bf4f00ceba2"
DEST = Path(__file__).resolve().parents[1] / "models" / "speaker" / NAME


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    if DEST.exists() and sha256(DEST) == SHA256:
        print(f"already present: {DEST}")
        return 0
    DEST.parent.mkdir(parents=True, exist_ok=True)
    partial = DEST.with_suffix(".part")
    print(f"downloading {URL}")
    urllib.request.urlretrieve(URL, partial)
    actual = sha256(partial)
    if actual != SHA256:
        partial.unlink(missing_ok=True)
        print(f"checksum mismatch: {actual}", file=sys.stderr)
        return 1
    partial.replace(DEST)
    print(f"saved {DEST} ({DEST.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
