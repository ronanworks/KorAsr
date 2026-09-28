"""Safe, deterministic paths for per-session course materials."""

from pathlib import Path
from uuid import UUID

from backend.config import settings


# Keep test and production archives separate whenever RAW_AUDIO_DIR differs.
# For example, data/raw-local-test stores documents in data/raw-local-test-materials.
_raw_audio_dir = Path(settings.raw_audio_dir)
MATERIALS_DIR = _raw_audio_dir.with_name(f"{_raw_audio_dir.name}-materials")


def canonical_uuid(value: str) -> str:
    try:
        if str(UUID(value)) != value:
            raise ValueError("non-canonical UUID")
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid material or session ID") from exc
    return value


def material_path(session_id: str, material_id: str, extension: str) -> Path:
    """Never include a user supplied filename in a filesystem path."""
    canonical_uuid(session_id)
    canonical_uuid(material_id)
    if extension not in {".pdf", ".pptx", ".docx", ".txt", ".md"}:
        raise ValueError("Unsupported material extension")
    path = MATERIALS_DIR / session_id / f"{material_id}{extension}"
    # A local symlink in the material tree must not redirect reads/deletes to
    # files outside the configured root.
    if not path.resolve().is_relative_to(MATERIALS_DIR.resolve()):
        raise ValueError("Material path escapes storage directory")
    return path
