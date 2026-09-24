from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import imageio_ffmpeg

from .config import Settings


class MediaError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 422):
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(message)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for attempt in range(6):
        try:
            temp.replace(path)
            return
        except PermissionError:
            if os.name != "nt" or attempt == 5:
                raise
            time.sleep(0.02 * (attempt + 1))


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _duration_ms(stderr: str) -> int | None:
    match = re.search(r"Duration:\s*(\d{2}):(\d{2}):(\d{2}(?:\.\d+)?)", stderr)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return round((int(hours) * 3600 + int(minutes) * 60 + float(seconds)) * 1000)


def _video_dimensions(stderr: str) -> tuple[int, int] | None:
    for line in stderr.splitlines():
        if "Video:" not in line:
            continue
        match = re.search(r"\b(\d{2,5})x(\d{2,5})(?=[,\s])", line)
        if match:
            return int(match.group(1)), int(match.group(2))
    return None


def probe_video(path: Path) -> dict[str, Any]:
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    try:
        process = subprocess.run(
            [ffmpeg, "-nostdin", "-hide_banner", "-i", str(path), "-map", "0:v:0", "-frames:v", "1", "-f", "null", "-"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise MediaError("VIDEO_PROBE_TIMEOUT", "读取视频信息超时，请换一个较短的视频。") from exc
    stderr = process.stderr or ""
    if "Video:" not in stderr:
        raise MediaError("VIDEO_DECODE_FAILED", "无法从该文件读取视频画面，请选择可播放的 MP4 或 MOV。")
    duration_ms = _duration_ms(stderr)
    dimensions = _video_dimensions(stderr)
    if duration_ms is None or not dimensions:
        raise MediaError("VIDEO_METADATA_UNAVAILABLE", "无法读取视频时长或画面尺寸。")
    width, height = dimensions
    rotation_match = re.search(r"rotation of (-?\d+)", stderr, re.IGNORECASE)
    rotation = int(rotation_match.group(1)) % 360 if rotation_match else 0
    if rotation in {90, 270}:
        width, height = height, width
    return {"duration_ms": duration_ms, "width": width, "height": height, "rotation": rotation}


def extract_frames(video_path: Path, frame_set_id: str, interval_ms: int,
                   metadata: dict[str, Any], settings: Settings) -> dict[str, Any]:
    expected = math.ceil(metadata["duration_ms"] / interval_ms)
    if expected < 1 or expected > settings.max_frames:
        raise MediaError("FRAME_LIMIT_EXCEEDED", f"该抽帧间隔会产生 {expected} 张图片，最多支持 {settings.max_frames} 张。")

    frame_set_dir = settings.frames_dir / frame_set_id
    frame_set_dir.mkdir(parents=True, exist_ok=False)
    temp_dir = frame_set_dir / "tmp"
    temp_dir.mkdir()
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    rate = 1000 / interval_ms
    rate_text = f"{rate:g}"
    vf = (
        f"fps=fps={rate_text}:start_time=0:round=up,"
        "scale=w=1024:h=1024:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1"
    )
    pattern = str(temp_dir / "image-%03d.jpg")
    args = [
        ffmpeg, "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-i", str(video_path),
        "-map", "0:v:0", "-vf", vf, "-an", "-sn", "-dn", "-frames:v", str(settings.max_frames),
        "-q:v", "3", pattern,
    ]
    try:
        result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90, check=False)
        if result.returncode != 0:
            raise MediaError("FRAME_EXTRACTION_FAILED", "抽取视频画面失败，请检查视频是否可正常解码。", status_code=422)
        generated = sorted(temp_dir.glob("image-*.jpg"))
        if len(generated) != expected:
            raise MediaError("FRAME_COUNT_MISMATCH", f"预期抽取 {expected} 张图片，实际抽取 {len(generated)} 张；本次实验已停止，避免 A/B 使用不完整画面。")

        frames = []
        for index, source in enumerate(generated):
            frame_id = f"f{index:03d}"
            destination = frame_set_dir / f"{frame_id}.jpg"
            shutil.move(str(source), str(destination))
            image_width, image_height = _jpeg_size(destination)
            frames.append({
                "frame_id": frame_id,
                "sample_time_ms": index * interval_ms,
                "source_time_ms": None,
                "timestamp_ms": index * interval_ms,
                "width": image_width,
                "height": image_height,
                "image_sha256": file_sha256(destination),
                "image_path": destination.relative_to(settings.data_dir).as_posix(),
                "size_bytes": destination.stat().st_size,
            })
        shutil.rmtree(temp_dir, ignore_errors=True)
        return {
            "frame_set_id": frame_set_id,
            "duration_ms": metadata["duration_ms"],
            "sample_interval_ms": interval_ms,
            "timestamp_basis": "nominal",
            "max_sampled_time_ms": frames[-1]["timestamp_ms"],
            "tail_unobserved_ms": max(0, metadata["duration_ms"] - frames[-1]["timestamp_ms"]),
            "frames": frames,
        }
    except Exception:
        shutil.rmtree(frame_set_dir, ignore_errors=True)
        raise


def _jpeg_size(path: Path) -> tuple[int, int]:
    with path.open("rb") as stream:
        if stream.read(2) != b"\xff\xd8":
            raise MediaError("INVALID_FRAME_IMAGE", "视频解码器返回了无效的 JPEG 图片。", 500)
        while True:
            marker_prefix = stream.read(1)
            if not marker_prefix:
                break
            if marker_prefix != b"\xff":
                continue
            marker = stream.read(1)
            while marker == b"\xff":
                marker = stream.read(1)
            if marker in {b"\xd8", b"\xd9"} or not marker:
                continue
            length_bytes = stream.read(2)
            if len(length_bytes) != 2:
                break
            length = int.from_bytes(length_bytes, "big")
            if marker in {b"\xc0", b"\xc1", b"\xc2", b"\xc3", b"\xc5", b"\xc6", b"\xc7", b"\xc9", b"\xca", b"\xcb", b"\xcd", b"\xce", b"\xcf"}:
                body = stream.read(5)
                if len(body) == 5:
                    height = int.from_bytes(body[1:3], "big")
                    width = int.from_bytes(body[3:5], "big")
                    return width, height
                break
            stream.seek(length - 2, 1)
    raise MediaError("INVALID_FRAME_IMAGE", "无法读取抽帧图片尺寸。", 500)
