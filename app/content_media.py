from __future__ import annotations

import math
import subprocess
import uuid
from pathlib import Path
from typing import Any

import imageio_ffmpeg

from .config import Settings
from .media import file_sha256


def opening_sample_times(duration_ms: int, max_frames: int = 20) -> list[int]:
    if duration_ms <= 0 or max_frames <= 0:
        return []
    duration_ms = min(duration_ms, 60_000)
    opening = list(range(0, min(duration_ms, 3000), 500))
    if duration_ms <= 3500 or len(opening) >= max_frames:
        return opening[:max_frames]
    span = duration_ms - 3000
    later_count = min(max_frames - len(opening), max(1, math.ceil(span / 3000)))
    # Avoid asking the decoder for many near-duplicate frames in a short clip.
    # Keep the last seek at least one second before nominal EOF, where low-FPS
    # videos are more likely to have a decodable frame.
    last_seek = max(3000, duration_ms - 1000)
    later = [3000 + round((last_seek - 3000) * (i + 1) / later_count) for i in range(later_count)]
    return sorted(set(opening + later))[:max_frames]


def extract_content_frames(video_path: Path, analysis_dir: Path, duration_ms: int, settings: Settings) -> dict[str, Any]:
    frame_set_id = "cfs_" + uuid.uuid4().hex
    frame_dir = analysis_dir / "frames"
    frame_dir.mkdir(parents=True, exist_ok=False)
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    frames = []
    for index, target_ms in enumerate(opening_sample_times(duration_ms, settings.max_frames)):
        frame_id = f"f{index:03d}"
        destination = frame_dir / f"{frame_id}.jpg"
        args = [
            ffmpeg, "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{target_ms / 1000:.3f}",
            "-i", str(video_path), "-frames:v", "1", "-vf",
            "scale=w=1024:h=1024:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1",
            "-q:v", "3", str(destination),
        ]
        try:
            result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=25, check=False)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"抽取 {frame_id} 超时。") from exc
        if result.returncode or not destination.is_file() or destination.stat().st_size < 100:
            raise RuntimeError(f"无法抽取 {frame_id}。")
        frames.append({
            "frame_id": frame_id, "timestamp_ms": target_ms, "source_time_ms": None,
            "sha256": file_sha256(destination), "relative_path": f"frames/{frame_id}.jpg",
        })
    return {
        "frame_set_id": frame_set_id, "duration_ms": duration_ms, "timestamp_basis": "nominal",
        "sample_interval_ms": None, "frames": frames,
    }
