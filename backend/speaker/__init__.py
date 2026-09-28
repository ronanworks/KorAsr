"""Online speaker diarization: CAM++ embeddings (CPU) + one online tracker per session."""
from backend.speaker.embedding import SpeakerEmbedder, get_embedder, speech_duration_s
from backend.speaker.tracker import OnlineSpeakerTracker, SpeakerAssignment

__all__ = ["SpeakerEmbedder", "get_embedder", "speech_duration_s", "OnlineSpeakerTracker", "SpeakerAssignment"]
