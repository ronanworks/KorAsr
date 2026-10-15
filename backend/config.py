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
    # Requests per minute this server sends to Moonshot: live translation (one per sentence
    # and target), per-minute polish, outline and summaries share it. The account allows
    # 100/min for the whole organisation (429 "request reached organization max RPM: 100"),
    # so 60 leaves room for other users of the key. 0 = no limit.
    moonshot_max_rpm: int = 60

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
    polish_reasr: bool = True   # re-recognise each speaker run with more context before the minute polish
    # Mandarin clips: "sensevoice" (sherpa-onnx, CPU; fewer errors than Whisper in the
    # 09-29 comparison) or "whisper". Korean always uses Whisper.
    asr_zh_engine: str = "sensevoice"
    sensevoice_model_dir: str = "models/asr/sensevoice-2024-07-17"
    sensevoice_threads: int = 4

    vad_threshold: float = 0.5
    vad_min_silence_ms: int = 600                  # down from 1000 — faster sentence boundary detection
    vad_min_speech_ms: int = 250
    # 小声增强 (backend/asr/vad_gain.py): gain on the copy of the audio that Silero VAD sees,
    # never on the recording or the audio Whisper hears. Each connection starts with the
    # default; the client switches it ({"action": "vad_gain"}). Off by default: on the 10-02
    # class it found many quiet student turns, on 09-28 about 30 % of the new sentences were wrong.
    vad_input_gain_max_db: float = 12.0            # strength when switched on
    vad_input_gain_default: bool = False

    # Occasional classroom languages (extra_languages, e.g. en/ru) need clear evidence:
    # Whisper p(L) ≥ min_prob and ≥ min_ratio × the best frequent language, and ≥ min_speech_s
    # of speech after trimming silence; while the previous sentence was already L, a plain
    # majority with p(L) ≥ sticky_min_prob suffices. Chosen on the 10-02 class
    # (data/eval/2026-10-05/multilang/design.md §8.6): 30/48 English clips shown, 0 saved
    # Korean/Chinese sentences changed. 0.7 is the conservative alternative for min_prob.
    langid_extra_min_prob: float = 0.5
    langid_extra_min_ratio: float = 2.0
    langid_extra_min_speech_s: float = 1.0
    langid_extra_sticky_min_prob: float = 0.2
    # Japanese as an occasional language needs stronger evidence: Whisper hears Korean as Japanese.
    # The 10-02, 09-29 and 09-28 classes replayed with ko,zh + en,ru,ja (data/eval/2026-10-15/ja/
    # report.html): with the en/ru gate 3 saved Korean sentences became Japanese (p(ja) 0.77, 0.76,
    # 0.61; p(ko) 0.10–0.14) and 3 dropped clips became wrong Japanese lines; at 0.85 nothing changes
    # in any of the three classes (highest p(ja) of a dropped clip: 0.82). A Japanese turn continues
    # only on p(ja) ≥ 0.5, not 0.2: 33 saved Korean/Chinese sentences have p(ja) ≥ 0.2 and above
    # p(ko/zh), and would stay Japanese after a Japanese line; 3 have p(ja) ≥ 0.5.
    langid_ja_min_prob: float = 0.85
    langid_ja_min_ratio: float = 2.0
    langid_ja_sticky_min_prob: float = 0.5

    # Union type prevents pydantic-settings from attempting json.loads before the validator.
    # Substrings of short outputs. "구독" / "좋아요" alone dropped real speech
    # ("말씀을 드리면 구독료, 데이터센터 사용료"), so only the video-outro phrases are listed.
    hallucination_blacklist: Union[List[str], str] = [
        "자막", "시청해주셔서", "MBC", "SBS", "KBS",
        "구독과 좋아요", "구독 좋아요", "구독 부탁", "좋아요 부탁", "좋아요와 구독",
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

    # --- speaker diarization ---
    # CAM++ 声纹 + 在线聚类，CPU 推理；模型缺失或加载失败时自动关闭，不影响转写。
    # 相对路径按项目根解析（不是 CWD）。阈值由 scripts/eval_speaker.py 在标注集 + 87 分钟课堂录音上选出，
    # 依据写在 backend/speaker/tracker.py 默认值旁；改这里时同步那边的 DEFAULT_*。
    speaker_enabled: bool = True
    speaker_model_path: str = "models/speaker/3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx"
    speaker_threads: int = 2
    speaker_assign_threshold: float = 0.5   # 余弦 ≥ 此值归入最像的说话人
    speaker_new_threshold: float = 0.4      # 与所有人都 < 此值且够长才新建说话人
    speaker_min_new_s: float = 2.0          # 新建说话人所需的语音秒数（去掉首尾静音后）
    speaker_max_speakers: int = 8
    # --- end speaker diarization ---

    # --- diarization ---
    # 每分钟精修窗口的轮次级说话人分离（backend/speaker/diarize.py）：pyannote segmentation-3.0 ONNX
    # + 上面的 CAM++ 声纹 + 聚类，纯 CPU。模型缺失/加载失败时自动关闭（精修沿用实时句子标签）。
    # 模型下载：python scripts/download_diarization_model.py。阈值依据：scripts/eval_diarize.py。
    diarization_enabled: bool = True
    diarization_segmentation_model_path: str = "models/speaker/sherpa-onnx-pyannote-segmentation-3-0.onnx"
    diarization_threads: int = 2
    diarization_step_s: float = 2.5              # 10 s 分割块的滑动步长；5 s 更快但合成集/课堂都更差
    diarization_cluster_threshold: float = 0.55  # 1.5 s 小窗声纹的质心链接：余弦 ≥ 此值合并
    diarization_match_threshold: float = 0.5     # 窗口簇 ↔ 全局说话人质心：余弦 ≥ 此值视为同一人
    diarization_min_new_s: float = 4.0           # 窗口里匹配不上的簇至少说这么多秒才新建全局说话人
    # --- end diarization ---

    # extra="ignore" so env keys consumed elsewhere (RAW_AUDIO_DIR / HF_HOME /
    # EXTRA_CERT_SANS — read via os.environ in handler/start) don't trip pydantic.
    model_config = {"env_file": str(_ENV_FILE), "env_file_encoding": "utf-8", "extra": "ignore"}

settings = Settings()
