from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_local_env() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip().lstrip("\ufeff")
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        value = value.strip().strip("\"'")
        if key.strip():
            os.environ.setdefault(key.strip(), value)


_load_local_env()


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    model_base_url: str = os.environ.get("MODEL_BASE_URL", "https://api.deepseek.com").rstrip("/")
    model_id: str = os.environ.get("MODEL_ID", "deepseek-flash")
    model_api_key: str = os.environ.get("MODEL_API_KEY", "")
    model_timeout_seconds: int = env_int("MODEL_TIMEOUT_SECONDS", 120)
    asr_provider: str = os.environ.get("ASR_PROVIDER", "disabled").strip().lower()
    asr_model: str = os.environ.get("ASR_MODEL", "")
    asr_api_key: str = os.environ.get("ASR_API_KEY", "")
    asr_base_url: str = os.environ.get("ASR_BASE_URL", "https://dashscope.aliyuncs.com").rstrip("/")
    asr_timeout_seconds: int = env_int("ASR_TIMEOUT_SECONDS", 180)
    asr_poll_interval_seconds: int = env_int("ASR_POLL_INTERVAL_SECONDS", 3)
    asr_max_retries: int = env_int("ASR_MAX_RETRIES", 2)
    asr_language_hint: str = os.environ.get("ASR_LANGUAGE_HINT", "zh").strip()
    asr_enable_words: bool = os.environ.get("ASR_ENABLE_WORDS", "true").strip().lower() in {"1", "true", "yes", "on"}
    asr_media_mode: str = os.environ.get("ASR_MEDIA_MODE", "public_url").strip().lower()
    asr_public_base_url: str = os.environ.get("ASR_PUBLIC_BASE_URL", "").rstrip("/")
    content_fixture_enabled: bool = os.environ.get("CONTENT_FIXTURE_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
    ffprobe_path: str = os.environ.get("FFPROBE_PATH", "")
    max_upload_bytes: int = env_int("MAX_UPLOAD_BYTES", 100 * 1024 * 1024)
    max_video_seconds: int = env_int("MAX_VIDEO_SECONDS", 60)
    max_frames: int = env_int("MAX_FRAMES", 20)
    frame_concurrency: int = env_int("FRAME_CONCURRENCY", 3)
    host: str = os.environ.get("HOST", "127.0.0.1")
    port: int = env_int("PORT", 8765)
    app_base_path: str = "/" + os.environ.get("APP_BASE_PATH", "").strip("/") if os.environ.get("APP_BASE_PATH", "").strip("/") else ""
    app_read_only: bool = os.environ.get("APP_READ_ONLY", "false").strip().lower() in {"1", "true", "yes", "on"}

    @property
    def data_dir(self) -> Path:
        path = Path(os.environ.get("APP_DATA_DIR", str(ROOT / "data")))
        return path if path.is_absolute() else ROOT / path

    @property
    def videos_dir(self) -> Path:
        return self.data_dir / "videos"

    @property
    def comparisons_dir(self) -> Path:
        return self.data_dir / "comparisons"

    @property
    def frames_dir(self) -> Path:
        return self.data_dir / "frame_sets"

    @property
    def audio_dir(self) -> Path:
        return self.data_dir / "audio_assets"

    @property
    def transcriptions_dir(self) -> Path:
        return self.data_dir / "transcriptions"

    @property
    def transcripts_dir(self) -> Path:
        return self.data_dir / "transcripts"

    @property
    def content_analyses_dir(self) -> Path:
        return self.data_dir / "content_analyses"


settings = Settings()
