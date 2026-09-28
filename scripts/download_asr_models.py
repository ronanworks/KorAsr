"""Download SenseVoice-Small (int8, sherpa-onnx) used for Mandarin clips; verify checksums.

Source: official k2-fsa/sherpa-onnx GitHub release `asr-models` (~163 MB archive).
Run once from the project root:
    python scripts/download_asr_models.py
"""
import hashlib
import sys
import tarfile
import urllib.request
from pathlib import Path

ARCHIVE = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"
URL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/{ARCHIVE}.tar.bz2"
ARCHIVE_SHA256 = "7d1efa2138a65b0b488df37f8b89e3d91a60676e416f515b952358d83dfd347e"
FILES = {
    "model.int8.onnx": "c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51",
    "tokens.txt": "f449eb28dc567533d7fa59be34e2abca8784f771850c78a47fb731a31429a1dc",
}
DEST = Path(__file__).resolve().parents[1] / "models" / "asr" / "sensevoice-2024-07-17"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    if all((DEST / name).exists() and sha256(DEST / name) == digest for name, digest in FILES.items()):
        print(f"already present: {DEST}")
        return 0
    DEST.mkdir(parents=True, exist_ok=True)
    archive = DEST.parent / f"{ARCHIVE}.tar.bz2.part"
    print(f"downloading {URL}")
    urllib.request.urlretrieve(URL, archive)
    if sha256(archive) != ARCHIVE_SHA256:
        archive.unlink(missing_ok=True)
        print("archive checksum mismatch", file=sys.stderr)
        return 1
    with tarfile.open(archive, "r:bz2") as tar:
        for name in FILES:
            member = tar.getmember(f"{ARCHIVE}/{name}")
            with tar.extractfile(member) as src, (DEST / name).open("wb") as dst:
                dst.write(src.read())
    archive.unlink(missing_ok=True)
    for name, digest in FILES.items():
        if sha256(DEST / name) != digest:
            print(f"checksum mismatch: {name}", file=sys.stderr)
            return 1
    print(f"saved {DEST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
