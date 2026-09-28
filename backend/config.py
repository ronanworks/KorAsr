# backend/config.py
import json
from pathlib import Path
from pydantic import field_validator
from pydantic_settings import BaseSettings
from typing import List, Union

# 绝对路径定位 .env，避免 CWD 不在项目根（autostart / detached 启动时）
# 导致 pydantic 找不到文件、所有字段缺失抛 ValidationError。
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

class Settings(BaseSettings):
    moonshot_api_key: str
    moonshot_model: str = "kimi-k2.6"
    moonshot_base_url: str = "https://api.moonshot.cn/v1"

    # ASR 后端选择
    #   "faster_whisper" — CTranslate2 后端，CPU/CUDA 通用，4090 服务器走这个
    #   "mlx"            — Apple MLX 框架，走 Mac M-series GPU（Metal），仅 Apple Silicon 可用
    asr_backend: str = "faster_whisper"

    asr_model: str = "large-v3"                   # 短名（small/medium/large-v3/large-v3-turbo），各后端各自 map 到具体 repo
    asr_device: str = "cuda"
    asr_compute_type: str = "float16"
    asr_cpu_threads: int = 0                      # CPU 推理线程数（仅 faster_whisper）；0 = CT2 自动；Mac 设为 P-core 数（M4 Pro 设 8）
    asr_beam_size: int = 5                        # up from 3 — better accuracy, ~same latency on 4090
    asr_initial_prompt: str = ""  # Only add proven course-specific terms; generic text caused prompt echo.
    asr_initial_prompt_zh: str = ""
    asr_max_buffer_s: float = 6.0
    asr_min_buffer_s: float = 0.5
    asr_no_speech_threshold: float = 0.6          # was hardcoded 0.7 in transcriber; exposed here
    asr_compression_ratio_threshold: float = 2.2  # discard overly repetitive outputs (hallucination signal)
    asr_log_prob_threshold: float = -1.0           # discard very low-confidence segments
    asr_repetition_penalty: float = 1.1            # penalise repeated tokens at decoder level
    polish_condition_on_previous_text: bool = False  # avoid long-form Whisper repetition loops

    vad_threshold: float = 0.5
    vad_min_silence_ms: int = 600                  # down from 1000 — faster sentence boundary detection
    vad_min_speech_ms: int = 250

    # Union type prevents pydantic-settings from attempting json.loads before the validator
    hallucination_blacklist: Union[List[str], str] = [
        "자막", "시청해주셔서", "구독", "좋아요", "MBC", "SBS", "KBS",
        "기상캐스터", "다음 영상에서 만나요", "시청자 여러분",
        "Korean university lecture", "이것은 한국 대학교",
        "자막 제공", "번역 제공", "cc 자막", "한국어 자막",
        "감사합니다", "안녕하세요", "시청해 주셔서",
    ]

    @field_validator("hallucination_blacklist", mode="before")
    @classmethod
    def parse_comma_separated(cls, v):
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                if isinstance(parsed, list):
                    return parsed
            except (json.JSONDecodeError, ValueError):
                pass
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    database_url: str = "sqlite:///./data/korasr.db"
    raw_audio_dir: str = "data/raw"
    extra_cert_sans: str = ""

    # extra="ignore" so env keys consumed elsewhere (RAW_AUDIO_DIR / HF_HOME /
    # EXTRA_CERT_SANS — read via os.environ in handler/start) don't trip pydantic.
    model_config = {"env_file": str(_ENV_FILE), "env_file_encoding": "utf-8", "extra": "ignore"}

settings = Settings()
