from __future__ import annotations

import asyncio
import hashlib
import json
import math
import logging
import re
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .config import ROOT, Settings, settings
from .audio import AudioError, extract_audio, probe_audio
from . import content_contracts
from .content_workflows import ContentWorkflow
from .media import MediaError, atomic_write_json, file_sha256, probe_video, read_json
from .model_client import DeepSeekClient
from .reporting import render_json, render_markdown
from .transcription import AudioAsset, TranscriptError, TranscriptionOptions, capabilities as asr_capabilities, parse_import, provider_for, standardize_result
from .workflows import execute_comparison, new_comparison_state, total_usage


ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,80}$")
FRAME_RE = re.compile(r"^f\d{3}$")
ALLOWED_SUFFIXES = {".mp4", ".mov"}


class TargetBrief(BaseModel):
    brand_name: str = ""
    product_name: str = ""
    theme: str = ""
    verified_selling_points: list[str] = Field(default_factory=list, max_length=20)
    replace_instructions: str = ""


class ComparisonCreate(BaseModel):
    video_id: str
    sample_interval_ms: int = Field(default=1000, ge=250, le=5000)
    target_brief: TargetBrief | None = None


class ReviewPayload(BaseModel):
    scores: dict[str, int | None] = Field(default_factory=dict)
    preference: Literal["A", "B", "equal", "neither", "insufficient", ""] = ""
    evidence_note: str = Field(default="", max_length=4000)
    reviewer: str = Field(default="", max_length=120)


class TranscriptionCreate(BaseModel):
    video_id: str
    language_hint: str | None = Field(default=None, max_length=40)
    vocabulary: list[str] = Field(default_factory=list, max_length=50)
    retry_of: str | None = None
    model_config = {"extra": "forbid"}


class TranscriptImport(BaseModel):
    video_id: str
    format: Literal["text", "json", "srt"]
    content: str | dict[str, Any]
    content_basis: Literal["speech", "subtitle", "unverified"] = "unverified"
    reviewed_by_human: bool = False
    derived_from_id: str | None = None
    model_config = {"extra": "forbid"}


class ContentAnalysisCreate(BaseModel):
    video_id: str
    comparison_id: str | None = None
    transcript_id: str | None = None
    transcription_job_id: str | None = None
    scope_hint: Literal["auto", "speech_content", "pure_display", "3d_showcase"] = "auto"
    origin_hint: Literal["unknown", "clip", "original"] = "unknown"
    retry_of: str | None = None
    model_config = {"extra": "forbid"}


class ComparisonContentAnalysisCreate(BaseModel):
    scope_hint: Literal["auto", "speech_content", "pure_display", "3d_showcase"] = "auto"
    origin_hint: Literal["unknown", "clip", "original"] = "unknown"
    model_config = {"extra": "forbid"}


class ContentReviewPayload(BaseModel):
    verdict: Literal["usable", "needs_revision", "unusable", "not_assessed"] = "not_assessed"
    dimensions: dict[str, int | None] = Field(default_factory=dict)
    notes: str = Field(default="", max_length=4000)
    reviewer: str = Field(default="", max_length=120)
    model_config = {"extra": "forbid"}


class Runtime:
    def __init__(self, config: Settings):
        self.settings = config
        self.client: DeepSeekClient | None = None
        self.tasks: dict[str, asyncio.Task] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.transcription_tasks: dict[str, asyncio.Task] = {}
        self.content_tasks: dict[str, asyncio.Task] = {}
        self.active_job = False

    def comparison_path(self, comparison_id: str) -> Path:
        return self.settings.comparisons_dir / comparison_id / "comparison.json"

    def load_comparison(self, comparison_id: str) -> dict[str, Any]:
        if not ID_RE.fullmatch(comparison_id):
            raise HTTPException(404, "未找到该对照任务。")
        path = self.comparison_path(comparison_id)
        if not path.is_file():
            raise HTTPException(404, "未找到该对照任务。")
        try:
            return read_json(path)
        except (OSError, ValueError):
            raise HTTPException(500, "对照任务记录无法读取。")

    async def save_comparison(self, comparison_id: str, state: dict[str, Any]) -> None:
        lock = self.locks.setdefault(comparison_id, asyncio.Lock())
        async with lock:
            await run_in_threadpool(atomic_write_json, self.comparison_path(comparison_id), state)

    def load_video(self, video_id: str) -> tuple[Path, dict[str, Any]]:
        if not ID_RE.fullmatch(video_id):
            raise HTTPException(404, "未找到该视频。")
        directory = self.settings.videos_dir / video_id
        metadata_path = directory / "metadata.json"
        if not metadata_path.is_file():
            raise HTTPException(404, "未找到该视频，请先上传。")
        metadata = read_json(metadata_path)
        video_path = directory / metadata.get("storage_name", "")
        if not video_path.is_file():
            raise HTTPException(404, "原视频文件已不存在。")
        return video_path, metadata

    def transcription_path(self, job_id: str) -> Path:
        return self.settings.transcriptions_dir / job_id / "job.json"

    def transcript_path(self, transcript_id: str) -> Path:
        return self.settings.transcripts_dir / transcript_id / "transcript.json"

    def content_path(self, analysis_id: str) -> Path:
        return self.settings.content_analyses_dir / analysis_id / "job.json"

    async def save_transcription(self, state: dict[str, Any]) -> None:
        await run_in_threadpool(atomic_write_json, self.transcription_path(state["transcription_job_id"]), state)

    async def save_content(self, state: dict[str, Any]) -> None:
        await run_in_threadpool(atomic_write_json, self.content_path(state["analysis_id"]), state)


runtime = Runtime(settings)
logger = logging.getLogger("frame_lab.content")


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.videos_dir.mkdir(parents=True, exist_ok=True)
    settings.frames_dir.mkdir(parents=True, exist_ok=True)
    settings.comparisons_dir.mkdir(parents=True, exist_ok=True)
    settings.audio_dir.mkdir(parents=True, exist_ok=True)
    settings.transcriptions_dir.mkdir(parents=True, exist_ok=True)
    settings.transcripts_dir.mkdir(parents=True, exist_ok=True)
    settings.content_analyses_dir.mkdir(parents=True, exist_ok=True)
    for path in settings.comparisons_dir.glob("*/comparison.json"):
        try:
            old_state = read_json(path)
            if old_state.get("status") in {"queued", "extracting", "running"}:
                old_state["status"] = "interrupted"
                old_state["stage"] = "interrupted"
                old_state["finished_at"] = time.time()
                old_state["error"] = {"code": "PROCESS_RESTARTED", "message": "服务重启时任务尚未结束；请新建任务重跑。"}
                for branch in old_state.get("branches", {}).values():
                    if branch.get("status") in {"pending", "running"}:
                        branch["status"] = "interrupted"
                        branch["stage"] = "interrupted"
                atomic_write_json(path, old_state)
        except (OSError, ValueError, TypeError):
            continue
    for directory, key in ((settings.transcriptions_dir, "transcription_job_id"), (settings.content_analyses_dir, "analysis_id")):
        for path in directory.glob("*/job.json"):
            try:
                old_job = read_json(path)
                if old_job.get("status") in {"queued", "probing", "extracting", "transcribing", "analyzing"}:
                    old_job.update({"status": "interrupted", "stage": "interrupted", "updated_at": _utc_now(),
                                    "finished_at": _utc_now(), "error": {"code": "PROCESS_RESTARTED", "message": "服务重启时任务尚未结束，请新建任务重试。", "retryable": True}})
                    atomic_write_json(path, old_job)
            except (OSError, ValueError, TypeError):
                continue
    runtime.client = DeepSeekClient(settings)
    try:
        yield
    finally:
        if runtime.client:
            await runtime.client.close()


app = FastAPI(title="视频拆解 A/B POC", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "app" / "static"), name="static")


@app.exception_handler(MediaError)
async def media_error_handler(_, exc: MediaError):
    return JSONResponse(status_code=exc.status_code, content={"error": {"code": exc.code, "message": exc.message}})


@app.exception_handler(AudioError)
@app.exception_handler(TranscriptError)
async def content_input_error_handler(_, exc):
    return JSONResponse(status_code=exc.status_code, content={"error": {"code": exc.code, "message": exc.message, "retryable": exc.status_code >= 500}})


def _ensure_writable() -> None:
    if settings.app_read_only:
        raise HTTPException(403, "此演示环境为只读模式。")


def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _asr_public_media_url(video_id: str) -> str | None:
    """Build the server-side read-only media URL used by URL-only ASR providers."""
    if settings.asr_media_mode != "public_url" or not settings.asr_public_base_url:
        return None
    return f"{settings.asr_public_base_url}/api/videos/{video_id}/audio"


def _transcription_job(job_id: str) -> dict[str, Any]:
    if not ID_RE.fullmatch(job_id):
        raise HTTPException(404, "未找到该转写任务。")
    path = runtime.transcription_path(job_id)
    if not path.is_file():
        raise HTTPException(404, "未找到该转写任务。")
    return read_json(path)


def _content_job(analysis_id: str) -> dict[str, Any]:
    if not ID_RE.fullmatch(analysis_id):
        raise HTTPException(404, "未找到该文案分析任务。")
    path = runtime.content_path(analysis_id)
    if not path.is_file():
        raise HTTPException(404, "未找到该文案分析任务。")
    return read_json(path)


def _store_transcript(transcript: dict[str, Any]) -> Path:
    path = runtime.transcript_path(transcript["transcript_id"])
    content_contracts.validate_transcript(transcript)
    atomic_write_json(path, transcript)
    return path


def _derive_subtitle_transcript_from_comparison(comparison: dict[str, Any]) -> dict[str, Any]:
    """Turn existing frame OCR/visible-text evidence into an explicitly non-ASR transcript.

    This is intentionally limited to text already observed by the visual workflow. It does
    not infer speech, and the resulting transcript stays marked as subtitle/manual evidence.
    """
    video_id = comparison.get("video_id")
    manifest = comparison.get("manifest") or {}
    duration_ms = int(manifest.get("duration_ms") or (comparison.get("video_metadata") or {}).get("duration_ms") or 0)
    frames = manifest.get("frames") or []
    observations = {item.get("frame_id"): item for item in (comparison.get("branches", {}).get("A", {}).get("frames") or [])}
    raw_segments: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for index, frame in enumerate(frames):
        frame_id = frame.get("frame_id")
        claim = (observations.get(frame_id) or {}).get("visible_text") or {}
        text = str(claim.get("value") or "").strip()
        if not text or claim.get("basis") == "unknown":
            continue
        start_ms = int(frame.get("timestamp_ms") or 0)
        next_start = int(frames[index + 1].get("timestamp_ms") or duration_ms) if index + 1 < len(frames) else duration_ms
        if current and current["text"] == text and start_ms <= current["end_ms"]:
            current["end_ms"] = max(current["end_ms"], next_start)
            current["frame_ids"].append(frame_id)
            continue
        if current:
            raw_segments.append(current)
        current = {"text": text, "start_ms": start_ms, "end_ms": max(start_ms + 1, next_start), "frame_ids": [frame_id]}
    if current:
        raw_segments.append(current)
    if not raw_segments:
        raise HTTPException(422, "当前视觉结果没有可用的画面文字证据，无法启动文案工作流。")
    segments = [{"text": item["text"], "start_ms": item["start_ms"], "end_ms": min(duration_ms, item["end_ms"]), "words": []}
                for item in raw_segments]
    source_payload = {"comparison_id": comparison.get("comparison_id"), "video_id": video_id,
                      "segments": segments}
    transcript = parse_import(
        format="json", content={"segments": segments}, content_basis="subtitle", video_id=video_id,
        duration_ms=duration_ms, reviewed_by_human=False,
        input_sha256=hashlib.sha256(json.dumps(source_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest(),
    )
    transcript["derived_from_id"] = comparison.get("comparison_id")
    transcript["warnings"] = [
        "该文本由既有视觉分析中的画面文字证据整理，不代表音频转写或实际口播。",
        "时间范围来自抽帧时间点，不能视为字幕或口播的精确边界。",
    ]
    return transcript


@app.get("/", response_class=HTMLResponse)
async def index():
    page = (ROOT / "app" / "templates" / "index.html").read_text(encoding="utf-8")
    page = page.replace("__APP_BASE_PATH__", settings.app_base_path)
    page = page.replace("__APP_READ_ONLY__", "true" if settings.app_read_only else "false")
    return HTMLResponse(page)


@app.get("/api/health")
async def health():
    return {"status": "ok", "model_configured": bool(settings.model_api_key), "model_id": settings.model_id}


@app.post("/api/videos", status_code=201)
async def upload_video(file: UploadFile = File(...)):
    _ensure_writable()
    original_name = Path(file.filename or "video.mp4").name
    suffix = Path(original_name).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(415, "目前仅接收 MP4 或 MOV 文件。")

    video_id = "video_" + uuid.uuid4().hex
    video_dir = settings.videos_dir / video_id
    video_dir.mkdir(parents=True, exist_ok=False)
    storage_name = "original" + suffix
    destination = video_dir / storage_name
    size = 0
    try:
        with destination.open("wb") as output:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise HTTPException(413, f"文件超过本机 POC 限制（{settings.max_upload_bytes // (1024 * 1024)} MiB）。")
                output.write(chunk)
        if size == 0:
            raise HTTPException(400, "上传的视频文件为空。")
        metadata = await run_in_threadpool(probe_video, destination)
        if metadata["duration_ms"] <= 0 or metadata["duration_ms"] > settings.max_video_seconds * 1000:
            raise HTTPException(422, f"视频时长须大于 0 且不超过 {settings.max_video_seconds} 秒。")
        recommended_interval = max(1000, math.ceil(metadata["duration_ms"] / (settings.max_frames * 250)) * 250)
        recommended_count = math.ceil(metadata["duration_ms"] / recommended_interval)
        result = {
            "video_id": video_id,
            "original_filename": original_name,
            "storage_name": storage_name,
            "size_bytes": size,
            "sha256": await run_in_threadpool(file_sha256, destination),
            **metadata,
            "recommended_sample_interval_ms": recommended_interval,
            "recommended_frame_count": recommended_count,
            "uploaded_at": time.time(),
        }
        atomic_write_json(video_dir / "metadata.json", result)
        return result
    except Exception:
        shutil.rmtree(video_dir, ignore_errors=True)
        raise
    finally:
        await file.close()


@app.post("/api/comparisons", status_code=202)
async def create_comparison(payload: ComparisonCreate):
    _ensure_writable()
    if not ID_RE.fullmatch(payload.video_id):
        raise HTTPException(404, "未找到该视频。")
    video_dir = settings.videos_dir / payload.video_id
    metadata_path = video_dir / "metadata.json"
    if not metadata_path.is_file():
        raise HTTPException(404, "未找到该视频，请先上传。")
    metadata = read_json(metadata_path)
    expected_frames = math.ceil(metadata["duration_ms"] / payload.sample_interval_ms)
    if expected_frames < 1 or expected_frames > settings.max_frames:
        raise HTTPException(422, f"当前采样间隔会得到 {expected_frames} 帧；允许范围为 1–{settings.max_frames} 帧。")
    if runtime.client is None:
        raise HTTPException(503, "模型客户端尚未启动。")
    if runtime.active_job or any(not task.done() for task in runtime.tasks.values()) or any(not task.done() for task in runtime.content_tasks.values()):
        raise HTTPException(409, "当前已有对照任务在运行，请等待结束后再创建新任务。")

    comparison_id = "cmp_" + uuid.uuid4().hex
    target = payload.target_brief.model_dump() if payload.target_brief else None
    state = new_comparison_state(comparison_id, payload.video_id, target, payload.sample_interval_ms, settings.model_id)
    state["video_metadata"] = {key: metadata.get(key) for key in ("original_filename", "duration_ms", "width", "height", "sha256")}
    state["configuration"] = {
        "model_base_url": settings.model_base_url,
        "model_id": settings.model_id,
        "temperature": 0.2,
        "thinking": "disabled",
        "image_detail": "high",
        "frame_concurrency": settings.frame_concurrency,
        "max_tokens": {"frame": 2048, "sequence": 16384, "report": 8192},
    }
    work_dir = settings.comparisons_dir / comparison_id
    work_dir.mkdir(parents=True, exist_ok=False)
    runtime.locks[comparison_id] = asyncio.Lock()
    runtime.active_job = True
    try:
        await runtime.save_comparison(comparison_id, state)
    except Exception:
        runtime.active_job = False
        raise

    async def save():
        await runtime.save_comparison(comparison_id, state)

    async def run_and_release():
        try:
            await execute_comparison(state, settings=settings, client=runtime.client, save=save)
        finally:
            runtime.active_job = False

    task = asyncio.create_task(run_and_release())
    runtime.tasks[comparison_id] = task
    task.add_done_callback(lambda _: runtime.tasks.pop(comparison_id, None))
    return {"comparison_id": comparison_id, "status": "queued", "status_url": f"/api/comparisons/{comparison_id}"}


@app.get("/api/comparisons")
async def list_comparisons():
    items = []
    for path in settings.comparisons_dir.glob("*/comparison.json"):
        try:
            state = read_json(path)
            metadata = state.get("video_metadata") or {}
            items.append({
                "comparison_id": state.get("comparison_id"),
                "video_id": state.get("video_id"),
                "original_filename": metadata.get("original_filename") or state.get("video_id"),
                "status": state.get("status"),
                "created_at": state.get("created_at"),
                "model_id": state.get("model_id"),
            })
        except (OSError, ValueError, TypeError):
            continue
    items.sort(key=lambda item: item.get("created_at") or 0, reverse=True)
    return {"items": items[:20]}


@app.get("/api/comparisons/{comparison_id}")
async def get_comparison(comparison_id: str):
    state = runtime.load_comparison(comparison_id)
    directory = settings.comparisons_dir / comparison_id
    review_path = directory / "review.json"
    review = read_json(review_path) if review_path.is_file() else None
    return {**state, "usage": total_usage(state), "review": review}


@app.get("/api/videos/{video_id}/metadata")
async def video_metadata(video_id: str):
    _, metadata = runtime.load_video(video_id)
    return {key: metadata.get(key) for key in ("video_id", "original_filename", "size_bytes", "sha256", "duration_ms", "width", "height", "uploaded_at")}


@app.get("/api/videos/{video_id}/content")
async def video_content(video_id: str):
    if not ID_RE.fullmatch(video_id):
        raise HTTPException(404, "未找到该视频。")
    directory = settings.videos_dir / video_id
    metadata_path = directory / "metadata.json"
    if not metadata_path.is_file():
        raise HTTPException(404, "未找到该视频。")
    metadata = read_json(metadata_path)
    path = directory / metadata["storage_name"]
    media_type = "video/quicktime" if path.suffix.lower() == ".mov" else "video/mp4"
    return FileResponse(path, media_type=media_type, filename=metadata.get("original_filename"), content_disposition_type="inline")


@app.get("/api/videos/{video_id}/audio")
async def video_audio_content(video_id: str):
    """Serve a cached mono WAV for URL-only transcription providers.

    This is a read-only media route. It does not expose local paths or accept
    writes; the first GET may create a deterministic cache file under the
    server-owned audio directory so DashScope can fetch a small audio asset.
    """
    video_path, _ = runtime.load_video(video_id)
    audio_path = settings.audio_dir / "public" / f"{video_id}.wav"
    if not audio_path.is_file() or audio_path.stat().st_size <= 44:
        info = await run_in_threadpool(probe_audio, video_path, settings)
        if not info.get("has_audio"):
            raise HTTPException(404, "该视频没有可用音轨。")
        await run_in_threadpool(extract_audio, video_path, info, audio_path)
    return FileResponse(audio_path, media_type="audio/wav", filename=f"{video_id}.wav", content_disposition_type="inline")


@app.get("/api/frame-sets/{frame_set_id}/frames/{frame_id}")
async def frame_content(frame_set_id: str, frame_id: str):
    if not ID_RE.fullmatch(frame_set_id) or not FRAME_RE.fullmatch(frame_id):
        raise HTTPException(404, "未找到该抽帧。")
    frame_dir = settings.frames_dir / frame_set_id
    manifest_path = frame_dir / "manifest.json"
    if not manifest_path.is_file():
        raise HTTPException(404, "未找到该抽帧。")
    manifest = read_json(manifest_path)
    if frame_id not in {frame["frame_id"] for frame in manifest.get("frames", [])}:
        raise HTTPException(404, "未找到该抽帧。")
    path = frame_dir / f"{frame_id}.jpg"
    if not path.is_file():
        raise HTTPException(404, "未找到该抽帧。")
    return FileResponse(path, media_type="image/jpeg")


@app.put("/api/comparisons/{comparison_id}/review")
async def save_review(comparison_id: str, payload: ReviewPayload):
    _ensure_writable()
    state = runtime.load_comparison(comparison_id)
    allowed_scores = {"static_description", "temporal_reasoning", "coverage", "summary_evidence", "prompt_usefulness", "adaptation"}
    if set(payload.scores) - allowed_scores:
        raise HTTPException(422, "评分字段包含未知项目。")
    if any(value is not None and not 1 <= value <= 5 for value in payload.scores.values()):
        raise HTTPException(422, "评分范围为 1–5，无法判断时留空。")
    review = {**payload.model_dump(), "updated_at": time.time(), "comparison_id": comparison_id}
    atomic_write_json(settings.comparisons_dir / comparison_id / "review.json", review)
    return {"saved": True, "review": review}


@app.get("/api/comparisons/{comparison_id}/export")
async def export_comparison(comparison_id: str, format: Literal["json", "md"] = "json"):
    state = runtime.load_comparison(comparison_id)
    review_path = settings.comparisons_dir / comparison_id / "review.json"
    review = read_json(review_path) if review_path.is_file() else None
    if format == "md":
        return Response(
            content=render_markdown(state, review), media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{comparison_id}.md"'},
        )
    return Response(
        content=render_json(state, review), media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{comparison_id}.json"'},
    )


@app.get("/api/capabilities")
async def get_capabilities():
    configured_provider = settings.asr_provider
    if configured_provider == "disabled":
        asr = {**asr_capabilities(settings), "provider": "disabled", "status": "not_connected"}
    else:
        asr = asr_capabilities(settings)
    return {
        "read_only": settings.app_read_only,
        "asr": asr,
        "analysis_model": {"configured": bool(settings.model_api_key), "model": settings.model_id},
        "transcript_import": {"enabled": not settings.app_read_only, "formats": ["text", "json", "srt"]},
        "limits": {"video_bytes": settings.max_upload_bytes, "video_duration_ms": settings.max_video_seconds * 1000,
                   "visual_frames": settings.max_frames, "transcript_characters": 100_000, "transcript_segments": 1000},
    }


@app.post("/api/transcriptions", status_code=202)
async def create_transcription(payload: TranscriptionCreate):
    _ensure_writable()
    video_path, video = runtime.load_video(payload.video_id)
    if runtime.active_job:
        raise HTTPException(409, "当前有一个媒体或分析任务在运行，请稍后重试。")
    job_id = "tj_" + uuid.uuid4().hex
    now = _utc_now()
    state = {"transcription_job_id": job_id, "video_id": payload.video_id, "status": "queued", "stage": "queued",
             "created_at": now, "updated_at": now, "finished_at": None, "retry_of": payload.retry_of,
             "language_hint": payload.language_hint, "vocabulary": payload.vocabulary,
             "audio_asset": None, "transcript_id": None, "error": None, "usage": None}
    atomic_write_json(runtime.transcription_path(job_id), state)
    runtime.active_job = True
    task = asyncio.create_task(_run_transcription(job_id, video_path, video, payload))
    runtime.transcription_tasks[job_id] = task
    task.add_done_callback(lambda _: runtime.transcription_tasks.pop(job_id, None))
    return {"transcription_job_id": job_id, "video_id": payload.video_id, "status": "queued", "status_url": f"/api/transcriptions/{job_id}"}


async def _run_transcription(job_id: str, video_path: Path, video: dict[str, Any], payload: TranscriptionCreate) -> None:
    path = runtime.transcription_path(job_id)
    state = read_json(path)
    handed_off = False
    try:
        state.update({"status": "probing", "stage": "probing", "updated_at": _utc_now()})
        await runtime.save_transcription(state)
        info = await run_in_threadpool(probe_audio, video_path, settings)
        if not info["has_audio"]:
            transcript = {"schema_version": "content-1.0", "kind": "transcript", "transcript_id": "tr_" + uuid.uuid4().hex,
                         "video_id": state["video_id"], "duration_ms": video["duration_ms"], "source": "media_probe",
                         "content_basis": "unverified", "outcome": "no_audio", "timing": "none", "timestamp_origin": "video_start",
                         "provider": None, "model": None, "language": None, "segments": [], "input_sha256": None,
                         "derived_from_id": None, "is_fixture": False, "reviewed_by_human": False, "warnings": []}
            transcript_path = _store_transcript(transcript)
            state.update({"status": "no_audio", "stage": "no_audio", "transcript_id": transcript["transcript_id"],
                          "probe": info, "error": None, "updated_at": _utc_now(), "finished_at": _utc_now()})
            await runtime.save_transcription(state)
            runtime.active_job = False
            handed_off = await _resume_waiting_content(job_id, transcript_path, transcript)
            return
        state["probe"] = info
        provider = provider_for(settings)
        capabilities = provider.capabilities()
        if not capabilities["configured"]:
            if settings.asr_provider == "disabled":
                code, message = "ASR_NOT_CONFIGURED", "音频转写尚未接入；请导入人工校对文本继续分析。"
            elif capabilities.get("status") == "media_url_not_configured":
                code, message = "ASR_MEDIA_URL_UNAVAILABLE", "真实 ASR 已配置，但尚未配置可公网访问的媒体地址。"
            else:
                code, message = "ASR_PROVIDER_NOT_CONFIGURED", "真实 ASR 配置不完整；请检查模型、密钥和服务地址。"
            state.update({"status": "waiting_provider", "stage": "waiting_provider", "updated_at": _utc_now(),
                          "error": {"code": code, "message": message, "retryable": False}})
            await runtime.save_transcription(state)
            return
        state.update({"status": "extracting", "stage": "extracting", "updated_at": _utc_now()})
        await runtime.save_transcription(state)
        audio_id = "aud_" + uuid.uuid4().hex
        asset_path = settings.audio_dir / payload.video_id / f"{audio_id}.wav"
        asset_meta = await run_in_threadpool(extract_audio, video_path, info, asset_path)
        import wave
        with wave.open(str(asset_path), "rb") as source:
            audio_duration = round(source.getnframes() * 1000 / source.getframerate())
        asset = AudioAsset(audio_id=audio_id, video_id=payload.video_id, path=str(asset_path), sha256=asset_meta["sha256"],
                           duration_ms=audio_duration, audio_offset_from_video_ms=asset_meta["audio_offset_from_video_ms"],
                           public_url=_asr_public_media_url(payload.video_id))
        state["audio_asset"] = {**{k: v for k, v in asset_meta.items() if k != "path"}, "audio_id": audio_id,
                                "duration_ms": audio_duration, "storage_name": asset_path.name}
        state.update({"status": "transcribing", "stage": "transcribing", "updated_at": _utc_now()})
        await runtime.save_transcription(state)
        result = await provider.transcribe(asset, TranscriptionOptions(payload.language_hint, tuple(payload.vocabulary)))
        state.update({"remote_job_id": result.get("remote_job_id"), "provider_request_id": result.get("request_id"),
                      "provider_record": result.get("provider_record"), "usage": result.get("usage") or None})
        await runtime.save_transcription(state)
        if settings.asr_provider == "fixture":
            # Fixture timestamps are relative to the synthetic provider input; they never stand for observed speech.
            asset_offset = 0
        else:
            asset_offset = asset.audio_offset_from_video_ms
        transcript = standardize_result(result=result, settings=settings, video_id=payload.video_id,
                                        duration_ms=video["duration_ms"], input_sha256=asset.sha256,
                                        offset_from_video_ms=asset_offset)
        if settings.asr_provider == "fixture":
            transcript["timing"] = "none"
            for item in transcript["segments"]:
                item["start_ms"] = item["end_ms"] = None
        transcript_path = _store_transcript(transcript)
        state.update({"status": "completed" if transcript["outcome"] == "text_available" else "no_speech",
                      "stage": "completed" if transcript["outcome"] == "text_available" else "no_speech",
                      "transcript_id": transcript["transcript_id"], "error": None,
                      "provider": transcript["provider"], "model": transcript["model"],
                      "updated_at": _utc_now(), "finished_at": _utc_now()})
        await runtime.save_transcription(state)
        runtime.active_job = False
        handed_off = await _resume_waiting_content(job_id, transcript_path, transcript)
    except (AudioError, TranscriptError) as exc:
        state.update({"status": "failed", "stage": "failed", "error": {"code": exc.code, "message": exc.message,
                      "retryable": exc.status_code >= 500}, "updated_at": _utc_now(), "finished_at": _utc_now()})
        await runtime.save_transcription(state)
    except Exception:
        state.update({"status": "failed", "stage": "failed", "error": {"code": "ASR_FAILED", "message": "音频处理或转写失败。",
                      "retryable": True}, "updated_at": _utc_now(), "finished_at": _utc_now()})
        await runtime.save_transcription(state)
    finally:
        if not handed_off:
            runtime.active_job = False


@app.get("/api/transcriptions/{job_id}")
async def get_transcription(job_id: str):
    return _transcription_job(job_id)


@app.post("/api/transcripts/import", status_code=201)
async def import_transcript(payload: TranscriptImport):
    _ensure_writable()
    _, video = runtime.load_video(payload.video_id)
    content_size = len(payload.content.encode("utf-8")) if isinstance(payload.content, str) else len(json.dumps(payload.content, ensure_ascii=False).encode("utf-8"))
    if content_size > 1_000_000:
        raise HTTPException(413, "导入内容最多 1 MiB。")
    transcript = parse_import(format=payload.format, content=payload.content, content_basis=payload.content_basis,
                              video_id=payload.video_id, duration_ms=video["duration_ms"],
                              reviewed_by_human=payload.reviewed_by_human,
                              input_sha256=hashlib.sha256((payload.content if isinstance(payload.content, str) else json.dumps(payload.content, ensure_ascii=False)).encode("utf-8")).hexdigest())
    if payload.derived_from_id:
        previous_path = runtime.transcript_path(payload.derived_from_id)
        if not previous_path.is_file() or read_json(previous_path).get("video_id") != payload.video_id:
            raise HTTPException(409, "被修订文本不存在或不属于该视频。")
        transcript["derived_from_id"] = payload.derived_from_id
    transcript_path = _store_transcript(transcript)
    return {"transcript_id": transcript["transcript_id"], "video_id": payload.video_id,
            "status": "completed", "transcript": transcript}


@app.get("/api/transcripts/{transcript_id}")
async def get_transcript(transcript_id: str):
    if not ID_RE.fullmatch(transcript_id):
        raise HTTPException(404, "未找到该文本。")
    path = runtime.transcript_path(transcript_id)
    if not path.is_file():
        raise HTTPException(404, "未找到该文本。")
    return read_json(path)


async def _resume_waiting_content(transcription_job_id: str, transcript_path: Path, transcript: dict[str, Any]) -> bool:
    # Explicitly associated content jobs become runnable only after the ASR result is persisted.
    launched = False
    for path in settings.content_analyses_dir.glob("*/job.json"):
        try:
            state = read_json(path)
        except (OSError, ValueError):
            continue
        if state.get("status") != "waiting_transcript" or state.get("transcription_job_id") != transcription_job_id:
            continue
        state["transcript_id"] = transcript["transcript_id"]
        if transcript["outcome"] not in {"text_available", "no_audio", "no_speech", "unintelligible"}:
            state.update({"status": "failed", "stage": "failed", "error": {"code": "TRANSCRIPT_FAILED", "message": "依赖的转写没有可用结果。", "retryable": True}})
            await runtime.save_content(state)
        elif not settings.model_api_key and state.get("scope_hint") not in {"pure_display", "3d_showcase"}:
            state.update({"status": "failed", "stage": "failed", "error": {"code": "MODEL_NOT_CONFIGURED", "message": "文案分析模型尚未配置。", "retryable": False}})
            await runtime.save_content(state)
        else:
            if runtime.active_job:
                state.update({"status": "failed", "stage": "failed", "error": {"code": "TASK_BUSY", "message": "转写完成时另一个任务正在运行；请新建分析任务。", "retryable": True}})
                await runtime.save_content(state)
            else:
                runtime.active_job = True
                task = asyncio.create_task(_launch_content_analysis(state, transcript, transcript_path))
                runtime.content_tasks[state["analysis_id"]] = task
                task.add_done_callback(lambda _, key=state["analysis_id"]: runtime.content_tasks.pop(key, None))
                launched = True
    return launched


@app.post("/api/content-analyses", status_code=202)
async def create_content_analysis(payload: ContentAnalysisCreate):
    _ensure_writable()
    video_path, video = runtime.load_video(payload.video_id)
    if payload.transcript_id and payload.transcription_job_id:
        raise HTTPException(422, "transcript_id 与 transcription_job_id 只能指定一个。")
    if payload.transcript_id:
        transcript_path = runtime.transcript_path(payload.transcript_id)
        if not transcript_path.is_file():
            raise HTTPException(404, "未找到该文本。")
        transcript = read_json(transcript_path)
        if transcript.get("video_id") != payload.video_id or transcript.get("duration_ms") != video["duration_ms"]:
            raise HTTPException(409, "文本与视频不匹配。")
        if payload.scope_hint not in {"pure_display", "3d_showcase"} and not settings.model_api_key:
            raise HTTPException(503, "文案分析模型尚未配置。", headers={"X-Error-Code": "MODEL_NOT_CONFIGURED"})
    elif payload.transcription_job_id:
        transcript_job = _transcription_job(payload.transcription_job_id)
        if transcript_job.get("video_id") != payload.video_id:
            raise HTTPException(409, "转写任务与视频不匹配。")
        transcript = None
        transcript_path = None
        if transcript_job.get("transcript_id"):
            transcript_path = runtime.transcript_path(transcript_job["transcript_id"])
            transcript = read_json(transcript_path) if transcript_path.is_file() else None
        if transcript is None and transcript_job.get("status") not in {"queued", "probing", "extracting", "transcribing", "waiting_provider"}:
            raise HTTPException(409, "关联的转写任务没有可用文本，请导入校对文本或重新转写。")
    elif payload.scope_hint in {"pure_display", "3d_showcase"}:
        transcript, transcript_path = None, None
    else:
        raise HTTPException(422, "请先完成语音转写或导入文本；纯展示 / 3D 可直接指定跳过。")

    if runtime.active_job and payload.scope_hint not in {"pure_display", "3d_showcase"} and transcript is not None:
        raise HTTPException(409, "当前已有一个媒体或分析任务在运行，请稍后重试。")
    analysis_id = "ca_" + uuid.uuid4().hex
    now = _utc_now()
    status = "queued" if transcript or payload.scope_hint in {"pure_display", "3d_showcase"} else "waiting_transcript"
    state = {"analysis_id": analysis_id, "video_id": payload.video_id, "comparison_id": payload.comparison_id,
             "transcript_id": transcript.get("transcript_id") if transcript else None,
             "transcription_job_id": payload.transcription_job_id, "scope_hint": payload.scope_hint, "origin_hint": payload.origin_hint,
             "status": status, "stage": status, "components": {}, "report": None, "model_calls": [], "usage": None,
             "retry_of": payload.retry_of, "error": None, "created_at": now, "updated_at": now, "finished_at": None}
    atomic_write_json(runtime.content_path(analysis_id), state)
    if transcript is None and status == "waiting_transcript":
        return {"analysis_id": analysis_id, "status": status, "status_url": f"/api/content-analyses/{analysis_id}"}
    if payload.scope_hint in {"pure_display", "3d_showcase"}:
        report = _skipped_report(state, video, transcript, {"available": False, "frame_set_id": None, "observations": []},
                                 {"scope_hint": payload.scope_hint, "origin_hint": payload.origin_hint}, None, None)
        content_contracts.validate_report(report)
        state.update({"status": "skipped", "stage": "skipped", "report": report, "updated_at": _utc_now(), "finished_at": _utc_now()})
        atomic_write_json(runtime.content_analyses_dir / analysis_id / "report.json", report)
        await runtime.save_content(state)
    else:
        runtime.active_job = True
        task = asyncio.create_task(_launch_content_analysis(state, transcript, transcript_path, video_path=video_path, video=video, pre_reserved=True))
        runtime.content_tasks[analysis_id] = task
        task.add_done_callback(lambda _: runtime.content_tasks.pop(analysis_id, None))
    return {"analysis_id": analysis_id, "status": state["status"], "status_url": f"/api/content-analyses/{analysis_id}"}


@app.post("/api/comparisons/{comparison_id}/content-analysis", status_code=202)
async def create_content_analysis_from_comparison(comparison_id: str, payload: ComparisonContentAnalysisCreate):
    """Launch the v0.3 workflow from an existing visual A/B result.

    The source transcript is derived only from visible-text claims already present in the
    comparison. It is stored as manual subtitle evidence so the report cannot present it as
    ASR or spoken copy.
    """
    _ensure_writable()
    comparison = runtime.load_comparison(comparison_id)
    if comparison.get("status") not in {"completed", "incomplete"}:
        raise HTTPException(409, "视觉对照任务尚未完成，暂时不能启动文案分析。")
    if not settings.model_api_key:
        raise HTTPException(503, "文案分析模型尚未配置。", headers={"X-Error-Code": "MODEL_NOT_CONFIGURED"})
    transcript = _derive_subtitle_transcript_from_comparison(comparison)
    transcript_path = _store_transcript(transcript)
    created = await create_content_analysis(ContentAnalysisCreate(
        video_id=comparison["video_id"], comparison_id=comparison_id, transcript_id=transcript["transcript_id"],
        scope_hint=payload.scope_hint, origin_hint=payload.origin_hint,
    ))
    created.update({"comparison_id": comparison_id, "transcript_id": transcript["transcript_id"],
                    "input_mode": "manual", "content_basis": "subtitle",
                    "transcript_path": str(transcript_path.relative_to(settings.data_dir))})
    return created


async def _launch_content_analysis(state: dict[str, Any], transcript: dict[str, Any] | None,
                                   transcript_path: Path | None, *, video_path: Path | None = None,
                                   video: dict[str, Any] | None = None, pre_reserved: bool = False) -> None:
    analysis_id = state["analysis_id"]
    if runtime.active_job and analysis_id not in runtime.content_tasks and not pre_reserved:
        state.update({"status": "failed", "stage": "failed", "error": {"code": "TASK_BUSY", "message": "另一个任务正在运行，请新建任务重试。", "retryable": True}, "finished_at": _utc_now()})
        await runtime.save_content(state)
        return
    if video is None:
        video_path, video = runtime.load_video(state["video_id"])
    if runtime.client is None:
        state.update({"status": "failed", "stage": "failed", "error": {"code": "MODEL_NOT_CONFIGURED", "message": "文案分析模型尚未配置。", "retryable": False}, "finished_at": _utc_now()})
        await runtime.save_content(state)
        return
    async def save(value: dict[str, Any]) -> None:
        await runtime.save_content(value)
    try:
        workflow = ContentWorkflow(settings, runtime.client, save)
        report = await workflow.run(state, video, video_path, transcript, transcript_path)
        state["report"] = report
    except Exception:
        logger.exception("Content analysis task failed: %s", analysis_id)
        state.update({"status": "failed", "stage": "failed", "error": {"code": "CONTENT_ANALYSIS_FAILED", "message": "文案分析失败，原视频和既有任务未修改。", "retryable": True},
                      "updated_at": _utc_now(), "finished_at": _utc_now()})
        await runtime.save_content(state)
    finally:
        runtime.active_job = False


@app.get("/api/content-analyses")
async def list_content_analyses(limit: int = 20, offset: int = 0, status: str | None = None,
                                format_tag: str | None = None, origin: str | None = None,
                                topic: str | None = None, input_mode: str | None = None,
                                video_id: str | None = None, comparison_id: str | None = None):
    limit = max(1, min(100, limit))
    offset = max(0, offset)
    found = []
    for path in settings.content_analyses_dir.glob("*/job.json"):
        try:
            job = read_json(path)
            report = job.get("report") or {}
            tags = report.get("tags") or {}
            if video_id and job.get("video_id") != video_id: continue
            if comparison_id and job.get("comparison_id") != comparison_id: continue
            if status and job.get("status") != status: continue
            if input_mode and report.get("input_mode") != input_mode: continue
            formats = {x.get("value") for x in tags.get("format_tags", [])}
            if format_tag and format_tag not in formats: continue
            origin_claim = tags.get("origin", {}).get("value")
            if origin and origin_claim != origin: continue
            topics = {x.get("value") for x in tags.get("topic_tags", [])}
            if topic and topic not in topics: continue
            found.append({"analysis_id": job.get("analysis_id"), "video_id": job.get("video_id"),
                          "comparison_id": job.get("comparison_id"),
                          "status": job.get("status"), "created_at": job.get("created_at"),
                          "input_mode": report.get("input_mode"), "scope": report.get("scope"),
                          "format_tags": sorted(x for x in formats if x), "origin": origin_claim,
                          "topic_tags": sorted(x for x in topics if x),
                          "summary": (report.get("copy") or {}).get("summary", {}).get("value")})
        except (OSError, ValueError, TypeError):
            continue
    found.sort(key=lambda x: (x.get("created_at") or "", x.get("analysis_id") or ""), reverse=True)
    return {"items": found[offset:offset + limit], "total": len(found), "limit": limit, "offset": offset}


@app.get("/api/content-analyses/{analysis_id}")
async def get_content_analysis(analysis_id: str):
    state = _content_job(analysis_id)
    return state


@app.get("/api/content-analyses/{analysis_id}/frames/{frame_id}")
async def get_content_frame(analysis_id: str, frame_id: str):
    if not ID_RE.fullmatch(analysis_id) or not FRAME_RE.fullmatch(frame_id):
        raise HTTPException(404, "未找到该画面。")
    path = settings.content_analyses_dir / analysis_id / "frames" / f"{frame_id}.jpg"
    if not path.is_file():
        raise HTTPException(404, "未找到该画面。")
    return FileResponse(path, media_type="image/jpeg")


@app.put("/api/content-analyses/{analysis_id}/review")
async def save_content_review(analysis_id: str, payload: ContentReviewPayload):
    _ensure_writable()
    state = _content_job(analysis_id)
    allowed = {"transcript_fidelity", "structure_usefulness", "tags_usefulness", "hook_evidence"}
    if set(payload.dimensions) - allowed or any(x is not None and not 1 <= x <= 5 for x in payload.dimensions.values()):
        raise HTTPException(422, "评价维度无效；评分范围为 1–5，无法判断时留空。")
    review = {**payload.model_dump(), "analysis_id": analysis_id, "updated_at": _utc_now()}
    atomic_write_json(settings.content_analyses_dir / analysis_id / "review.json", review)
    return {"saved": True, "review": review}


@app.get("/api/content-analyses/{analysis_id}/export")
async def export_content_analysis(analysis_id: str, format: Literal["json", "md"] = "json"):
    state = _content_job(analysis_id)
    report = state.get("report")
    if not report:
        raise HTTPException(409, "文案分析报告尚未生成。", headers={"X-Error-Code": "REPORT_NOT_READY"})
    review_path = settings.content_analyses_dir / analysis_id / "review.json"
    review = read_json(review_path) if review_path.is_file() else None
    if format == "json":
        content, media = json.dumps({"report": report, "review": review}, ensure_ascii=False, indent=2), "application/json; charset=utf-8"
        extension = "json"
    else:
        content, media = render_content_markdown(report, review), "text/markdown; charset=utf-8"
        extension = "md"
    return Response(content=content, media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="{analysis_id}.{extension}"'})


def render_content_markdown(report: dict[str, Any], review: dict[str, Any] | None) -> str:
    copy_result, tags, hook = report["copy"], report["tags"], report["hook"]
    lines = ["# 视频文案分析", "", f"- 来源：{report['input_mode']}", f"- 状态：{report['status']}", f"- 范围：{report['scope']}", "",
             "## 摘要", "", str(copy_result["summary"]["value"] or "无法判断"), "", "## 文案结构", ""]
    for segment in copy_result["segments"]:
        timeline = f"{segment['start_ms']}–{segment['end_ms']} ms" if segment["start_ms"] is not None else "时间未知"
        source_text = " ".join(x["text"] for x in report.get("transcript", {}).get("segments", []) if x["segment_id"] in segment["transcript_segment_ids"])
        lines.append(f"- **{timeline} · {', '.join(segment['roles'])}** {source_text}")
        lines.append(f"  - 分析：{segment['purpose']['value'] or '无法判断'}")
    lines += ["", "## 分类标签", "", "- 内容形式：" + (", ".join(x["value"] for x in tags["format_tags"]) or "未知"),
              "- 来源：" + str(tags["origin"]["value"] or "未知"), "- 主题：" + (", ".join(x["value"] for x in tags["topic_tags"]) or "未知"),
              "", "## 前三秒", "", f"- 口播摘录：{hook['spoken_excerpt']['value'] or '无法判断'}",
              f"- 屏幕文字：{hook['onscreen_text']['value'] or '无法判断'}",
              f"- 画面：{hook['visual_summary']['value'] or '无法判断'}",
              f"- 表达手法：{', '.join(x['type'] for x in hook['techniques']) or '未发现明确手法'}",
              f"- 说明：{hook['explanation']['value'] or '无法判断'}", "", "## 局限", ""]
    lines.extend(f"- {item}" for item in report["limitations"] or ["无特别说明。"])
    if review:
        lines += ["", "## 业务评价", "", f"- 结论：{review['verdict']}", f"- 备注：{review['notes'] or '无'}"]
    return "\n".join(lines) + "\n"
