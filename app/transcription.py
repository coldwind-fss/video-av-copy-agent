from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from .config import Settings


class TranscriptError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 422):
        self.code, self.message, self.status_code = code, message, status_code
        super().__init__(message)


@dataclass(frozen=True)
class AudioAsset:
    audio_id: str
    video_id: str
    path: str
    sha256: str
    duration_ms: int
    audio_offset_from_video_ms: int | None
    public_url: str | None = None


@dataclass(frozen=True)
class TranscriptionOptions:
    language_hint: str | None = None
    vocabulary: tuple[str, ...] = ()
    require_word_timestamps: bool = True


class TranscriptionProvider(Protocol):
    def capabilities(self) -> dict[str, Any]: ...
    async def transcribe(self, audio: AudioAsset, options: TranscriptionOptions) -> dict[str, Any]: ...


class DisabledProvider:
    def __init__(self, settings: Settings):
        self.settings = settings

    def capabilities(self) -> dict[str, Any]:
        return {
            "configured": False, "provider": "disabled", "model": None,
            "accepted_formats": ["wav"], "input_modes": [], "timestamp_granularity": "unknown",
            "status": "not_connected",
        }

    async def transcribe(self, audio: AudioAsset, options: TranscriptionOptions) -> dict[str, Any]:
        raise TranscriptError("ASR_NOT_CONFIGURED", "音频转写尚未接入。", 503)


class FixtureProvider:
    """Explicit deterministic provider used only for engineering acceptance."""

    def __init__(self, enabled: bool):
        self.enabled = enabled

    def capabilities(self) -> dict[str, Any]:
        return {
            "configured": self.enabled, "provider": "fixture", "model": "fixture-v1" if self.enabled else None,
            "accepted_formats": ["wav"], "input_modes": ["local_path"] if self.enabled else [],
            "timestamp_granularity": "sentence" if self.enabled else "unknown",
            "status": "development_only" if self.enabled else "disabled",
        }

    async def transcribe(self, audio: AudioAsset, options: TranscriptionOptions) -> dict[str, Any]:
        if not self.enabled:
            raise TranscriptError("FIXTURE_DISABLED", "开发联调 Provider 未开启。", 503)
        duration = audio.duration_ms
        sample = "这是用于工程联调的模拟转写文本。"
        if duration < 100:
            raise TranscriptError("AUDIO_TOO_SHORT", "音频太短，无法执行联调。")
        split = min(duration, max(100, duration // 2))
        segments = [{"text": sample, "start_ms": 0, "end_ms": split}]
        if duration - split >= 100:
            segments.append({"text": "第二段模拟文本。", "start_ms": split, "end_ms": duration})
        return {"provider": "fixture", "model": "fixture-v1", "language": options.language_hint or "zh", "segments": segments, "no_speech": False}


def provider_for(settings: Settings) -> TranscriptionProvider:
    if settings.asr_provider == "fixture":
        return FixtureProvider(settings.content_fixture_enabled)
    if settings.asr_provider == "aliyun_dashscope":
        from .providers.aliyun_asr import AliyunDashScopeFiletransProvider
        return AliyunDashScopeFiletransProvider(settings)
    return DisabledProvider(settings)


def capabilities(settings: Settings) -> dict[str, Any]:
    provider = provider_for(settings)
    return provider.capabilities()


def standardize_result(
    *, result: dict[str, Any], settings: Settings, video_id: str, duration_ms: int,
    input_sha256: str, offset_from_video_ms: int | None, transcript_id: str | None = None,
) -> dict[str, Any]:
    transcript_id = transcript_id or "tr_" + uuid.uuid4().hex
    raw_segments = result.get("segments") or []
    if result.get("no_speech") is True:
        if raw_segments:
            raise TranscriptError("ASR_RESPONSE_INVALID", "无人声结果仍包含转写文本。", 502)
        outcome, timing, content_basis = "no_speech", "none", "speech"
    elif raw_segments:
        outcome, content_basis = "text_available", "speech"
        timing = "sentence" if offset_from_video_ms is not None else "none"
    else:
        raise TranscriptError("ASR_EMPTY_RESULT", "识别服务没有返回文本或明确的无人声结果。", 502)
    normalized = []
    all_segments_have_words = True
    previous_end = 0
    for index, segment in enumerate(raw_segments):
        text = str(segment.get("text") or "").strip()
        start, end = segment.get("start_ms"), segment.get("end_ms")
        if not text:
            continue
        raw_words = segment.get("words") if isinstance(segment, dict) else []
        if not isinstance(raw_words, list):
            raw_words = []
        if not isinstance(start, int) or not isinstance(end, int) or not 0 <= start < end:
            timing = "none"
            start = end = None
        elif offset_from_video_ms is None:
            timing = "none"
            start = end = None
        else:
            start, end = start + offset_from_video_ms, end + offset_from_video_ms
            if start < 0 or end > duration_ms or start < previous_end:
                timing = "none"
                start = end = None
            else:
                previous_end = end
        words = []
        if start is not None and end is not None and raw_words:
            previous_word_end = start
            for word_index, word in enumerate(raw_words):
                if not isinstance(word, dict):
                    continue
                word_text = str(word.get("text") or "").strip()
                word_start, word_end = word.get("start_ms"), word.get("end_ms")
                if not word_text or not isinstance(word_start, int) or not isinstance(word_end, int):
                    continue
                word_start += offset_from_video_ms or 0
                word_end += offset_from_video_ms or 0
                if not start <= word_start < word_end <= end or word_start < previous_word_end:
                    words = []
                    break
                words.append({"word_id": f"w{index:03d}_{word_index:03d}", "text": word_text,
                              "start_ms": word_start, "end_ms": word_end})
                previous_word_end = word_end
        if not words:
            all_segments_have_words = False
        normalized.append({"segment_id": f"t{index:03d}", "text": text, "start_ms": start, "end_ms": end, "words": words})
    if outcome == "text_available" and not normalized:
        raise TranscriptError("ASR_EMPTY_RESULT", "识别服务没有返回可用文本。", 502)
    if timing == "none":
        for segment in normalized:
            segment["start_ms"] = segment["end_ms"] = None
            segment["words"] = []
    elif normalized and all_segments_have_words:
        timing = "word"
    else:
        timing = "sentence"
    return {
        "schema_version": "content-1.0", "kind": "transcript", "transcript_id": transcript_id,
        "video_id": video_id, "duration_ms": duration_ms,
        "source": "fixture" if settings.asr_provider == "fixture" else "asr", "content_basis": content_basis,
        "outcome": outcome, "timing": timing, "timestamp_origin": "video_start",
        "provider": result.get("provider"), "model": result.get("model"), "language": result.get("language"),
        "segments": normalized, "input_sha256": input_sha256, "derived_from_id": None,
        "is_fixture": settings.asr_provider == "fixture", "reviewed_by_human": False,
        "warnings": ["开发联调模拟文本，不是真实语音识别结果。"] if settings.asr_provider == "fixture" else [],
    }


def parse_import(
    *, format: str, content: Any, content_basis: str, video_id: str, duration_ms: int,
    reviewed_by_human: bool, input_sha256: str | None,
) -> dict[str, Any]:
    if content_basis not in {"speech", "subtitle", "unverified"}:
        raise TranscriptError("TRANSCRIPT_INVALID", "content_basis 只能是 speech、subtitle 或 unverified。", 422)
    raw_segments: list[dict[str, Any]] = []
    if format == "text":
        if not isinstance(content, str) or not content.strip():
            raise TranscriptError("TRANSCRIPT_INVALID", "文本内容不能为空。", 422)
        raw_segments = [{"text": content.strip(), "start_ms": None, "end_ms": None}]
        timing = "none"
    elif format == "json":
        if not isinstance(content, dict) or not isinstance(content.get("segments"), list):
            raise TranscriptError("TRANSCRIPT_INVALID", "JSON 文本需要 segments 数组。", 422)
        raw_segments = content["segments"]
        timing = "word" if raw_segments and all(isinstance(s, dict) and bool(s.get("words")) for s in raw_segments) else "sentence"
        if any(isinstance(s, dict) and s.get("words") for s in raw_segments) and timing != "word":
            raise TranscriptError("TRANSCRIPT_INVALID", "词级时间必须为每一段完整提供。", 422)
    elif format == "srt":
        if not isinstance(content, str):
            raise TranscriptError("TRANSCRIPT_INVALID", "SRT 内容必须是文本。", 422)
        raw_segments = _parse_srt(content)
        timing = "sentence"
    else:
        raise TranscriptError("UNSUPPORTED_TRANSCRIPT_FORMAT", "只支持 text、json 或 srt。", 415)
    if not raw_segments or len(raw_segments) > 1000:
        raise TranscriptError("TRANSCRIPT_INVALID", "文本段数须为 1–1000。", 422)
    if sum(len(str(s.get("text", ""))) for s in raw_segments) > 100_000:
        raise TranscriptError("TRANSCRIPT_TOO_LARGE", "转写文本最多 100000 个字符。", 413)
    segments, previous_end = [], 0
    for index, segment in enumerate(raw_segments):
        if not isinstance(segment, dict):
            raise TranscriptError("TRANSCRIPT_INVALID", f"第 {index + 1} 段格式无效。", 422)
        text = segment.get("text")
        if not isinstance(text, str) or not text.strip():
            raise TranscriptError("TRANSCRIPT_INVALID", f"第 {index + 1} 段内容为空。", 422)
        start, end = segment.get("start_ms"), segment.get("end_ms")
        if (start is None) != (end is None):
            raise TranscriptError("TIMESTAMP_INVALID", f"第 {index + 1} 段必须同时提供起止时间。", 422)
        if start is None:
            timing = "none"
        else:
            if not isinstance(start, int) or not isinstance(end, int) or not 0 <= start < end <= duration_ms:
                raise TranscriptError("TIMESTAMP_INVALID", f"第 {index + 1} 段时间超出视频范围。", 422)
            if start < previous_end:
                raise TranscriptError("TIMESTAMP_INVALID", "字幕 / 转写段不能逆序或重叠。", 422)
            previous_end = end
        raw_words = segment.get("words", []) if format == "json" else []
        if not isinstance(raw_words, list) or len(raw_words) > 1000:
            raise TranscriptError("TRANSCRIPT_INVALID", f"第 {index + 1} 段词级内容格式无效。", 422)
        words = []
        previous_word_end = start or 0
        for word_index, word in enumerate(raw_words):
            if not isinstance(word, dict) or not isinstance(word.get("text"), str) or not word["text"].strip():
                raise TranscriptError("TRANSCRIPT_INVALID", f"第 {index + 1} 段的第 {word_index + 1} 个词无效。", 422)
            word_start, word_end = word.get("start_ms"), word.get("end_ms")
            if start is None or not isinstance(word_start, int) or not isinstance(word_end, int) or not start <= word_start < word_end <= end or word_start < previous_word_end:
                raise TranscriptError("TIMESTAMP_INVALID", f"第 {index + 1} 段的词级时间无效。", 422)
            words.append({"word_id": f"w{index:03d}_{word_index:03d}", "text": word["text"].strip(),
                          "start_ms": word_start, "end_ms": word_end})
            previous_word_end = word_end
        segments.append({"segment_id": f"t{index:03d}", "text": text.strip(), "start_ms": start, "end_ms": end, "words": words})
    if timing == "word" and content_basis != "speech":
        raise TranscriptError("TRANSCRIPT_INVALID", "仅口播转写可以声明词级时间。", 422)
    if any(s["start_ms"] is None for s in segments):
        if any(s["start_ms"] is not None for s in segments):
            raise TranscriptError("TIMESTAMP_INVALID", "单份导入不能混合有时间与无时间的段落。", 422)
        timing = "none"
    return {
        "schema_version": "content-1.0", "kind": "transcript", "transcript_id": "tr_" + uuid.uuid4().hex,
        "video_id": video_id, "duration_ms": duration_ms, "source": "manual", "content_basis": content_basis,
        "outcome": "text_available", "timing": timing, "timestamp_origin": "video_start", "provider": None,
        "model": None, "language": None, "segments": segments, "input_sha256": input_sha256,
        "derived_from_id": None, "is_fixture": False, "reviewed_by_human": reviewed_by_human, "warnings": [],
    }


def _parse_srt(text: str) -> list[dict[str, Any]]:
    blocks = re.split(r"\r?\n\s*\r?\n", text.strip())
    result = []
    stamp = r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
    for block in blocks:
        lines = block.strip().splitlines()
        time_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if time_index is None:
            continue
        match = re.fullmatch(rf"\s*{stamp}\s*-->\s*{stamp}(?:\s+.*)?", lines[time_index])
        if not match:
            raise TranscriptError("TIMESTAMP_INVALID", "SRT 时间行格式无效。", 422)
        parts = [int(x) for x in match.groups()]
        start = ((parts[0] * 60 + parts[1]) * 60 + parts[2]) * 1000 + parts[3]
        end = ((parts[4] * 60 + parts[5]) * 60 + parts[6]) * 1000 + parts[7]
        content = "\n".join(lines[time_index + 1:]).strip()
        result.append({"text": content, "start_ms": start, "end_ms": end})
    return result
