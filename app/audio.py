from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import imageio_ffmpeg

from .config import Settings
from .media import file_sha256


class AudioError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 422):
        self.code, self.message, self.status_code = code, message, status_code
        super().__init__(message)


def _ffprobe(settings: Settings) -> str | None:
    if settings.ffprobe_path:
        return settings.ffprobe_path if Path(settings.ffprobe_path).is_file() else None
    found = shutil.which("ffprobe")
    if found:
        return found
    ffmpeg = Path(imageio_ffmpeg.get_ffmpeg_exe())
    sibling = ffmpeg.with_name("ffprobe.exe" if ffmpeg.suffix.lower() == ".exe" else "ffprobe")
    return str(sibling) if sibling.is_file() else None


def probe_audio(video_path: Path, settings: Settings) -> dict[str, Any]:
    ffprobe = _ffprobe(settings)
    if ffprobe:
        try:
            result = subprocess.run(
                [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(video_path)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, check=False,
            )
            if result.returncode != 0:
                raise AudioError("MEDIA_PROBE_FAILED", "无法读取媒体音轨信息。")
            raw = json.loads(result.stdout)
            streams = raw.get("streams") or []
            video = next((s for s in streams if s.get("codec_type") == "video"), None)
            audio = [s for s in streams if s.get("codec_type") == "audio"]
            video_start = _float_or_none(video.get("start_time")) if video else None
            if not audio:
                return {"has_audio": False, "tracks": [], "video_start_time_ms": _ms(video_start), "probe_method": "ffprobe"}
            tracks = []
            for stream in audio:
                audio_start = _float_or_none(stream.get("start_time"))
                offset = audio_start - video_start if audio_start is not None and video_start is not None else None
                tracks.append({
                    "stream_index": int(stream["index"]),
                    "codec": stream.get("codec_name"),
                    "sample_rate": _int_or_none(stream.get("sample_rate")),
                    "channels": _int_or_none(stream.get("channels")),
                    "start_time_ms": _ms(audio_start),
                    "offset_from_video_ms": _ms(offset),
                    "alignment_known": offset is not None,
                })
            return {"has_audio": True, "tracks": tracks, "video_start_time_ms": _ms(video_start), "probe_method": "ffprobe"}
        except subprocess.TimeoutExpired as exc:
            raise AudioError("MEDIA_PROBE_TIMEOUT", "读取媒体音轨信息超时。", 408) from exc
        except (OSError, ValueError, KeyError, TypeError) as exc:
            if isinstance(exc, AudioError):
                raise
            raise AudioError("MEDIA_PROBE_FAILED", "无法解析媒体音轨信息。") from exc

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    try:
        result = subprocess.run(
            [ffmpeg, "-nostdin", "-hide_banner", "-i", str(video_path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AudioError("MEDIA_PROBE_TIMEOUT", "读取媒体音轨信息超时。", 408) from exc
    stderr = result.stderr or ""
    if "Invalid data found" in stderr or "No such file" in stderr:
        raise AudioError("MEDIA_PROBE_FAILED", "无法读取媒体音轨信息。")
    # The bundled ffmpeg does not include ffprobe on every platform. For the
    # common case where the container advertises one start time, preserve the
    # zero-point instead of discarding all timing precision.
    start_match = re.search(r"\bstart:\s*(-?\d+(?:\.\d+)?)", stderr)
    container_start_ms = _ms(_float_or_none(start_match.group(1))) if start_match else None
    tracks = []
    for match in re.finditer(r"Stream #\d+:(\d+)(?:\[[^\]]+\])?(?:\([^)]*\))?: Audio:\s*([^,\s]+).*?(\d+)\s*Hz,\s*([^,\n]+)", stderr):
        tracks.append({
            "stream_index": None, "audio_ordinal": len(tracks), "codec": match.group(2),
            "sample_rate": int(match.group(3)), "channel_layout": match.group(4).strip(),
            "channels": None, "start_time_ms": container_start_ms, "offset_from_video_ms": 0 if container_start_ms is not None else None,
            "alignment_known": container_start_ms is not None,
        })
    return {"has_audio": bool(tracks), "tracks": tracks, "video_start_time_ms": container_start_ms, "probe_method": "bundled_ffmpeg"}


def extract_audio(video_path: Path, audio_info: dict[str, Any], destination: Path) -> dict[str, Any]:
    if not audio_info.get("has_audio") or not audio_info.get("tracks"):
        raise AudioError("NO_AUDIO_TRACK", "视频没有可提取的音轨。")
    destination.parent.mkdir(parents=True, exist_ok=True)
    track = audio_info["tracks"][0]
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    stream_index = track.get("stream_index")
    selector = f"0:{stream_index}" if stream_index is not None else f"0:a:{track.get('audio_ordinal', 0)}"
    command = [
        ffmpeg, "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-i", str(video_path),
        "-map", selector, "-vn", "-sn", "-dn", "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le", "-f", "wav", str(destination),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, check=False)
    except subprocess.TimeoutExpired as exc:
        raise AudioError("AUDIO_EXTRACTION_TIMEOUT", "提取音轨超时。", 408) from exc
    if result.returncode != 0 or not destination.is_file() or destination.stat().st_size <= 44:
        destination.unlink(missing_ok=True)
        raise AudioError("AUDIO_EXTRACTION_FAILED", "提取音轨失败。")
    return {
        "path": str(destination), "sha256": file_sha256(destination), "format": "wav",
        "sample_rate": 16000, "channels": 1, "stream_index": stream_index,
        "audio_offset_from_video_ms": track.get("offset_from_video_ms"),
        "alignment_known": bool(track.get("alignment_known")),
    }


def _float_or_none(value: Any) -> float | None:
    try:
        result = float(value)
        return result if result == result else None
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _ms(value: float | None) -> int | None:
    return round(value * 1000) if value is not None else None
