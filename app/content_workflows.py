from __future__ import annotations

import base64
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from . import content_contracts as cc
from .config import Settings
from .content_media import extract_content_frames
from .media import atomic_write_json, file_sha256, read_json
from .model_client import DeepSeekClient, ModelAPIError

logger = logging.getLogger("frame_lab.content")


def _claim(value: str | None, basis: str, evidence: list[dict[str, Any]] | None = None, note: str = "") -> dict[str, Any]:
    return {"value": value, "basis": basis, "evidence": evidence or [], "note": note}


def _unknown(note: str) -> dict[str, Any]:
    return _claim(None, "unknown", [], note)


def _prompt_hash(prompt: str) -> str:
    return cc.sha256_json(prompt)


def _canonicalize_metadata_evidence(value: Any, path: str = "") -> list[dict[str, str]]:
    """Map common model aliases back to the metadata IDs supplied by the workflow.

    The prompt exposes ``scope_hint`` and ``origin_hint`` as the only metadata
    evidence IDs. Models sometimes make the relationship explicit by returning
    ``metadata_scope_hint`` or ``metadata_origin_hint``. These aliases refer to
    the same supplied values, so canonicalize them before report validation and
    keep an audit entry in the model call record.
    """
    aliases = {"metadata_scope_hint": "scope_hint", "metadata_origin_hint": "origin_hint"}
    normalizations: list[dict[str, str]] = []
    if isinstance(value, dict):
        evidence = value.get("evidence")
        if isinstance(evidence, list):
            for index, item in enumerate(evidence):
                if not isinstance(item, dict) or item.get("type") != "metadata":
                    continue
                old_id = item.get("id")
                new_id = aliases.get(old_id) if isinstance(old_id, str) else None
                if new_id:
                    item["id"] = new_id
                    normalizations.append({
                        "path": f"{path}.evidence[{index}].id" if path else f"evidence[{index}].id",
                        "action": "canonicalize_metadata_evidence_id",
                        "from": old_id,
                        "to": new_id,
                    })
        for key, child in value.items():
            if key == "evidence":
                continue
            child_path = f"{path}.{key}" if path else key
            normalizations.extend(_canonicalize_metadata_evidence(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            normalizations.extend(_canonicalize_metadata_evidence(child, f"{path}[{index}]"))
    return normalizations


class ContentWorkflow:
    def __init__(self, settings: Settings, client: DeepSeekClient, save: Callable[[dict[str, Any]], Any]):
        self.settings, self.client, self.save = settings, client, save

    async def _call(self, *, analysis_dir: Path, job: dict[str, Any], node: str, prompt: str,
                    contract: dict[str, Any], user_text: str, images: list[dict[str, Any]] | None = None,
                    max_tokens: int = 6000) -> dict[str, Any]:
        schema_text = json.dumps(contract, ensure_ascii=False, separators=(",", ":"))
        system = cc.COMMON_PROMPT.replace("{{OUTPUT_CONTRACT_JSON}}", schema_text) + "\n\n" + prompt
        user_content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
        for item in images or []:
            data = base64.b64encode(Path(item["path"]).read_bytes()).decode("ascii")
            user_content.append({"type": "text", "text": f"帧 {item['frame_id']}，视频播放时间 {item['timestamp_ms']} 毫秒。"})
            user_content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{data}", "detail": "high"}})
        records = []
        content = ""
        for attempt in range(2):
            active_system = system
            if attempt:
                repair = cc.REPAIR_PROMPT.replace("{{VALIDATION_ERRORS_JSON}}", json.dumps(errors[:12], ensure_ascii=False))
                active_system += "\n\n" + repair
            response = await self.client.complete(
                active_system, user_content, max_tokens=max_tokens,
                input_text=user_text,
                call_context={
                    "node_id": node, "analysis_id": job["analysis_id"],
                    "workflow_version": cc.WORKFLOW_VERSION, "prompt_version": cc.PROMPT_VERSION,
                    "taxonomy_version": cc.TAXONOMY_VERSION, "schema_version": "content-1.0",
                    "prompt_sha256": _prompt_hash(system), "input_text_sha256": _prompt_hash(user_text),
                    "schema_attempt": attempt + 1,
                },
            )
            content = response.content
            record = {**response.record, "node_id": node, "returned_model": response.model_id, "schema_attempt": attempt + 1}
            records.append(record)
            job.setdefault("model_calls", []).append(record)
            try:
                parsed = json.loads(content)
                normalizations = []
                if node == "copy_tags" and isinstance(parsed, dict) and isinstance(parsed.get("copy"), dict):
                    copy_value = parsed["copy"]
                    for field in ("start_ms", "end_ms", "structure_pattern"):
                        if field in copy_value:
                            copy_value.pop(field)
                            normalizations.append({"path": f"copy.{field}", "action": "discard_server_derived_value"})
                    for index, segment in enumerate(copy_value.get("segments", [])):
                        if not isinstance(segment, dict):
                            continue
                        for field in ("start_ms", "end_ms"):
                            if field in segment:
                                segment.pop(field)
                                normalizations.append({"path": f"copy.segments[{index}].{field}", "action": "discard_server_derived_value"})
                normalizations.extend(_canonicalize_metadata_evidence(parsed))
                if normalizations:
                    record["application_normalizations"] = normalizations
                cc.validate_node(node, parsed)
                errors = []
            except (json.JSONDecodeError, cc.ContentContractError) as exc:
                errors = [str(exc)[:4000]]
                atomic_write_json(analysis_dir / "calls" / f"{node}-{attempt + 1}-{record['call_id']}.json", {
                    "record": record, "raw_response": content[:100_000], "validation_errors": errors,
                })
                if attempt:
                    raise cc.ContentContractError(errors)
                continue
            atomic_write_json(analysis_dir / "calls" / f"{node}-{attempt + 1}-{record['call_id']}.json", {
                "record": record, "raw_response": content[:100_000], "normalized_output": parsed,
            })
            return parsed
        raise cc.ContentContractError(errors or ["Model returned no valid JSON."])

    async def run(self, job: dict[str, Any], video_meta: dict[str, Any], video_path: Path,
                  transcript: dict[str, Any] | None, transcript_file: Path | None) -> dict[str, Any]:
        analysis_dir = self.settings.content_analyses_dir / job["analysis_id"]
        analysis_dir.mkdir(parents=True, exist_ok=True)
        (analysis_dir / "calls").mkdir(parents=True, exist_ok=True)
        job["status"], job["stage"], job["updated_at"] = "analyzing", "visual", _now()
        job["components"] = {name: {"status": "pending", "error": None} for name in ("visual", "copy_tags", "hook")}
        await self.save(job)
        duration_ms = video_meta["duration_ms"]
        metadata = {"scope_hint": job["scope_hint"], "origin_hint": job["origin_hint"]}
        limitations: list[str] = []
        errors: list[dict[str, str]] = []
        manifest: dict[str, Any] | None = None
        visual = {"available": False, "frame_set_id": None, "observations": []}
        outputs: dict[str, Any] = {}
        try:
            manifest = await _thread(lambda: extract_content_frames(video_path, analysis_dir, duration_ms, self.settings))
            images = [{**frame, "path": str(analysis_dir / frame["relative_path"])} for frame in manifest["frames"]]
            request_text = json.dumps({
                "video_duration_ms": duration_ms, "timestamp_basis": "nominal",
                "frames": [{"frame_id": x["frame_id"], "timestamp_ms": x["timestamp_ms"], "source_time_ms": None} for x in images],
            }, ensure_ascii=False)
            raw_visual = await self._call(
                analysis_dir=analysis_dir, job=job, node="visual", prompt=cc.VISUAL_PROMPT,
                contract=cc.VISUAL_CONTRACT, user_text=request_text, images=images, max_tokens=8000,
            )
            by_id = {frame["frame_id"]: frame for frame in manifest["frames"]}
            observations = []
            for i, row in enumerate(raw_visual["observations"]):
                frame = by_id.get(row["frame_id"])
                if not frame:
                    raise cc.ContentContractError(["visual response referenced a frame outside the request"])
                observations.append({"observation_id": f"vf{i:03d}", "frame_id": frame["frame_id"],
                                     "timestamp_ms": frame["timestamp_ms"],
                                     "description": row["description"], "onscreen_text": row["onscreen_text"],
                                     "limitations": row["limitations"]})
            if {x["frame_id"] for x in observations} != set(by_id):
                raise cc.ContentContractError(["visual response did not cover the extracted frame set"])
            visual = {"available": True, "frame_set_id": manifest["frame_set_id"], "observations": observations}
            job["components"]["visual"] = {"status": "completed", "error": None}
            job["frame_manifest"] = manifest
            await self.save(job)
        except Exception as exc:
            logger.exception("Content component %s failed for %s", "visual", job["analysis_id"])
            errors.append({"component": "visual", "code": _error_code(exc), "message": _safe_message(exc)})
            job["components"]["visual"] = {"status": "failed", "error": errors[-1]}
            limitations.append("画面分析未完成；该报告不包含完整视觉证据。")
            await self.save(job)

        job["stage"] = "copy_tags"
        await self.save(job)
        try:
            raw = await self._call(
                analysis_dir=analysis_dir, job=job, node="copy_tags", prompt=cc.COPY_PROMPT,
                contract=cc.COPY_TAGS_CONTRACT,
                user_text=json.dumps({"transcript": transcript, "visual": visual, "metadata": metadata}, ensure_ascii=False),
                max_tokens=6000,
            )
            outputs.update(raw)
            transcript_segments = {x["segment_id"]: x for x in (transcript or {}).get("segments", [])}
            for segment in raw["copy"]["segments"]:
                selected = [transcript_segments[x] for x in segment["transcript_segment_ids"] if x in transcript_segments]
                if selected and transcript and transcript["timing"] != "none":
                    segment["start_ms"], segment["end_ms"] = selected[0]["start_ms"], selected[-1]["end_ms"]
                else:
                    segment["start_ms"] = segment["end_ms"] = None
            role_labels = {"pain_point": "痛点", "audience": "人群", "product_intro": "商品介绍",
                           "selling_point": "卖点", "proof": "依据", "scenario": "场景",
                           "offer": "福利", "call_to_action": "行动", "other": "其他"}
            raw["copy"]["structure_pattern"] = " → ".join(
                " + ".join(role_labels[role] for role in segment["roles"])
                for segment in raw["copy"]["segments"]
            ) if raw["copy"]["status"] == "completed" else None
            job["components"]["copy_tags"] = {"status": "completed", "error": None}
        except Exception as exc:
            logger.exception("Content component %s failed for %s", "copy_tags", job["analysis_id"])
            errors.append({"component": "copy_tags", "code": _error_code(exc), "message": _safe_message(exc)})
            outputs["copy"] = _unavailable_copy(_safe_message(exc))
            outputs["tags"] = {"format_tags": [], "origin": _unknown("标签模型没有返回可校验结果。"), "topic_tags": []}
            job["components"]["copy_tags"] = {"status": "failed", "error": errors[-1]}
            limitations.append("文案结构 / 标签分析未完成。")
        await self.save(job)

        if _auto_excluded(transcript, outputs.get("copy"), outputs.get("tags"), visual, job["scope_hint"]):
            job["components"]["hook"] = {"status": "skipped", "error": None}
            report = _skipped_report(job, video_meta, transcript, visual, metadata, outputs.get("tags"), manifest)
            cc.validate_report(report)
            return await self._finish(job, report, "skipped", None, analysis_dir, errors)

        job["stage"] = "hook"
        await self.save(job)
        try:
            window_end = min(3000, duration_ms)
            opening_visual = [x for x in visual["observations"] if x["timestamp_ms"] < window_end]
            excerpt, excerpt_evidence, boundary = _opening_excerpt(transcript, window_end)
            user_hook = {
                "window_start_ms": 0, "window_end_ms": window_end,
                "timing_precision": (transcript or {}).get("timing", "none"),
                "available_spoken_excerpt": excerpt,
                "transcript_evidence": excerpt_evidence,
                "boundary_unresolved": boundary,
                "visual_evidence": opening_visual,
                "limitations": ["只使用窗口内证据。", "画面为稀疏抽样。"],
            }
            hook_raw = await self._call(
                analysis_dir=analysis_dir, job=job, node="hook", prompt=cc.HOOK_PROMPT,
                contract=cc.HOOK_CONTRACT, user_text=json.dumps(user_hook, ensure_ascii=False), max_tokens=3500,
            )
            if transcript and transcript.get("content_basis") == "speech" and excerpt:
                spoken = _claim(excerpt, "observed", excerpt_evidence, "逐字摘录，仅包含边界内的完整句 / 词。")
            elif transcript and transcript.get("content_basis") == "speech" and excerpt == "":
                spoken = _claim("前三秒没有完整可引用的口播句 / 词。", "observed",
                                [{"type": "transcript_result", "id": transcript["transcript_id"], "quote": None}],
                                "完整转写中没有句 / 词完整落在前三秒窗口内。")
            else:
                spoken = _unknown("没有可校验的前三秒口播时间；字幕和来源未确认文本不作为口播。")
            timing = (transcript or {}).get("timing", "none")
            hook_limitations = list(user_hook["limitations"])
            if timing == "none":
                hook_limitations.append("转写无可用视频时间戳。")
            if transcript and transcript.get("content_basis") != "speech":
                hook_limitations.append("文本来源不是经过转写的口播。")
            if boundary:
                hook_limitations.append("句子 / 词跨越窗口边界，无法将整段归入前三秒。")
            full_hook = {
                "status": "available" if transcript and transcript.get("content_basis") == "speech" and timing != "none" and not boundary else "limited",
                "window_start_ms": 0, "window_end_ms": window_end,
                "timing_precision": timing, "spoken_excerpt": spoken,
                "onscreen_text": hook_raw["onscreen_text"], "visual_summary": hook_raw["visual_summary"],
                "techniques": hook_raw["techniques"], "explanation": hook_raw["explanation"],
                "boundary_unresolved": boundary, "limitations": hook_limitations,
            }
            job["components"]["hook"] = {"status": "completed" if full_hook["status"] == "available" else "unavailable", "error": None}
            outputs["hook"] = full_hook
            status = "completed" if outputs["copy"]["status"] == "completed" and full_hook["status"] == "available" else "partial"
            report = _make_report(job, video_meta, transcript, visual, outputs, metadata, status, limitations, self.settings, manifest, job.get("model_calls", []))
            report["provenance"]["transcript_sha256"] = file_sha256(transcript_file) if transcript_file and transcript_file.is_file() else None
            cc.validate_report(report)
            return await self._finish(job, report, status, transcript_file, analysis_dir, errors)
        except Exception as exc:
            logger.exception("Content component %s failed for %s", "hook", job["analysis_id"])
            errors.append({"component": "hook", "code": _error_code(exc), "message": _safe_message(exc)})
            job["components"]["hook"] = {"status": "failed", "error": errors[-1]}
            limitations.append("前三秒分析未完成。")
            fallback_hook = _unavailable_hook(duration_ms, "前三秒模型没有返回可校验结果。")
            outputs["hook"] = fallback_hook
            report = _make_report(job, video_meta, transcript, visual, outputs, metadata, "partial", limitations, self.settings, manifest, job.get("model_calls", []))
            report["provenance"]["transcript_sha256"] = file_sha256(transcript_file) if transcript_file and transcript_file.is_file() else None
            cc.validate_report(report)
            return await self._finish(job, report, "partial", transcript_file, analysis_dir, errors)

    async def _finish(self, job: dict[str, Any], report: dict[str, Any], status: str,
                      transcript_file: Path | None, analysis_dir: Path, errors: list[dict[str, str]]) -> dict[str, Any]:
        atomic_write_json(analysis_dir / "report.json", report)
        job.update({"status": status, "stage": status, "report": report, "error": errors or None,
                    "updated_at": _now(), "finished_at": _now(),
                    "usage": _usage(job.get("model_calls", [])),
                    "transcript_sha256": file_sha256(transcript_file) if transcript_file and transcript_file.is_file() else None})
        await self.save(job)
        return report


def _make_report(job: dict[str, Any], video: dict[str, Any], transcript: dict[str, Any] | None,
                 visual: dict[str, Any], outputs: dict[str, Any], metadata: dict[str, Any], status: str,
                 limitations: list[str], settings: Settings, manifest: dict[str, Any] | None,
                 calls: list[dict[str, Any]]) -> dict[str, Any]:
    input_mode = transcript["source"] if transcript and transcript["source"] in {"asr", "manual", "fixture"} else "missing"
    if transcript and transcript.get("content_basis") == "subtitle":
        limitations = list(dict.fromkeys([*limitations,
            "文本来自既有视觉结果中的画面文字证据，不代表音频转写或实际口播。",
            "画面文字的时间范围由抽帧时间点近似得到。",
        ]))
    tags = outputs.get("tags") or {"format_tags": [], "origin": _unknown("没有足够依据确定素材来源。"), "topic_tags": []}
    hook = outputs.get("hook") or _unavailable_hook(video["duration_ms"], "尚未分析前三秒。")
    copy = outputs.get("copy") or _unavailable_copy("没有生成文案结构结果。")
    evidence = []
    if transcript and transcript.get("outcome") == "no_audio":
        evidence.append({"type": "transcript_result", "id": transcript["transcript_id"], "quote": None})
    if transcript and transcript.get("segments") and copy["summary"]["basis"] != "unknown":
        evidence.append({"type": "transcript_segment", "id": transcript["segments"][0]["segment_id"], "quote": None})
    if not evidence:
        evidence = [{"type": "metadata", "id": "scope_hint", "quote": None}]
    scope_claim = _claim("素材已进入文案分析。", "inferred", evidence)
    return {
        "schema_version": "content-1.0", "kind": "content_report", "analysis_id": job["analysis_id"],
        "video_id": job["video_id"], "duration_ms": video["duration_ms"], "status": status,
        "input_mode": input_mode, "scope": "in_scope", "scope_reason": scope_claim, "metadata": metadata,
        "transcript": transcript, "visual": visual, "copy": copy, "tags": tags, "hook": hook,
        "limitations": limitations, "provenance": {
            "workflow_version": cc.WORKFLOW_VERSION, "prompt_version": cc.PROMPT_VERSION,
            "taxonomy_version": cc.TAXONOMY_VERSION, "video_sha256": video["sha256"],
            "transcript_sha256": None,
            "returned_model": calls[-1].get("returned_model") if calls else None,
            "generated_at": _now(), "model_call_ids": [x.get("call_id", "unknown") for x in calls],
            "is_fixture": input_mode == "fixture",
        },
    }


def _skipped_report(job: dict[str, Any], video: dict[str, Any], transcript: dict[str, Any] | None,
                    visual: dict[str, Any], metadata: dict[str, Any], tags: dict[str, Any] | None,
                    manifest: dict[str, Any] | None) -> dict[str, Any]:
    reason = "用户明确将此视频标记为纯展示或 3D 展示。"
    basis = "observed"
    evidence = [{"type": "metadata", "id": "scope_hint", "quote": None}]
    if job["scope_hint"] == "auto":
        reason, basis = "无语音且抽样画面支持无实质口播的纯展示 / 3D 内容；离散采样仍可能漏字幕。", "inferred"
        evidence = [{"type": "transcript_result", "id": transcript["transcript_id"], "quote": None}]
        evidence += [{"type": "visual_observation", "id": x["observation_id"], "quote": None} for x in visual["observations"]]
    tags = tags or {"format_tags": [], "origin": _unknown("没有足够来源信息。"), "topic_tags": []}
    window_end = min(3000, video["duration_ms"])
    input_mode = transcript["source"] if transcript and transcript["source"] in {"asr", "manual", "fixture"} else "missing"
    return {
        "schema_version": "content-1.0", "kind": "content_report", "analysis_id": job["analysis_id"],
        "video_id": job["video_id"], "duration_ms": video["duration_ms"], "status": "skipped",
        "input_mode": input_mode,
        "scope": "excluded", "scope_reason": _claim(reason, basis, evidence), "metadata": metadata,
        "transcript": transcript, "visual": visual,
        "copy": {"status": "skipped", "summary": _unknown("该类型素材按范围规则跳过文案拆解。"), "segments": [], "structure_pattern": None},
        "tags": tags,
        "hook": {"status": "skipped", "window_start_ms": 0, "window_end_ms": window_end, "timing_precision": (transcript or {}).get("timing", "none"),
                 "spoken_excerpt": _unknown("素材按范围规则跳过前三秒文案分析。"), "onscreen_text": _unknown("素材按范围规则跳过。"),
                 "visual_summary": _unknown("素材按范围规则跳过。"), "techniques": [],
                 "explanation": _unknown("素材按范围规则跳过。"), "boundary_unresolved": False, "limitations": [reason]},
        "limitations": [reason], "provenance": {
            "workflow_version": cc.WORKFLOW_VERSION, "prompt_version": cc.PROMPT_VERSION,
            "taxonomy_version": cc.TAXONOMY_VERSION, "video_sha256": video["sha256"],
            "transcript_sha256": None, "returned_model": None, "generated_at": _now(),
            "model_call_ids": [], "is_fixture": input_mode == "fixture",
        },
    }


def _auto_excluded(transcript: dict[str, Any] | None, copy: dict[str, Any] | None,
                   tags: dict[str, Any] | None, visual: dict[str, Any], scope_hint: str) -> bool:
    if scope_hint in {"pure_display", "3d_showcase"}:
        return True
    if scope_hint != "auto" or not transcript or transcript["outcome"] not in {"no_audio", "no_speech"} or not tags:
        return False
    formats = {x["value"] for x in tags.get("format_tags", [])}
    if not formats.intersection({"product_demo", "animation_3d"}) or not visual.get("available"):
        return False
    if any(obs.get("onscreen_text") for obs in visual["observations"]):
        return False
    return copy is None or copy.get("status") == "unavailable"


def _opening_excerpt(transcript: dict[str, Any] | None, window_end: int) -> tuple[str | None, list[dict[str, Any]], bool]:
    if not transcript or transcript.get("outcome") != "text_available" or transcript.get("content_basis") != "speech":
        return None, [], False
    selected: list[dict[str, Any]] = []
    boundary = False
    if transcript["timing"] == "word":
        for segment in transcript["segments"]:
            for word in segment["words"]:
                if word["start_ms"] < window_end < word["end_ms"]:
                    boundary = True
                elif word["end_ms"] <= window_end:
                    selected.append({"type": "transcript_word", "id": word["word_id"], "quote": word["text"]})
        return " ".join(x["quote"] for x in selected) if selected else "", selected, boundary
    if transcript["timing"] == "sentence":
        for segment in transcript["segments"]:
            if segment["start_ms"] is None:
                return None, [], True
            if segment["start_ms"] < window_end < segment["end_ms"]:
                boundary = True
            elif segment["end_ms"] <= window_end:
                selected.append({"type": "transcript_segment", "id": segment["segment_id"], "quote": segment["text"]})
        return "\n".join(x["quote"] for x in selected) if selected else "", selected, boundary
    return None, [], bool(transcript.get("segments"))


def _unavailable_copy(note: str) -> dict[str, Any]:
    return {"status": "unavailable", "summary": _unknown(note), "segments": [], "structure_pattern": None}


def _unavailable_hook(duration_ms: int, note: str) -> dict[str, Any]:
    return {"status": "limited", "window_start_ms": 0, "window_end_ms": min(3000, duration_ms),
            "timing_precision": "none", "spoken_excerpt": _unknown(note), "onscreen_text": _unknown(note),
            "visual_summary": _unknown(note), "techniques": [], "explanation": _unknown(note),
            "boundary_unresolved": True, "limitations": [note]}


def _usage(calls: list[dict[str, Any]]) -> dict[str, int]:
    result = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "call_count": len(calls)}
    for call in calls:
        usage = call.get("usage") or {}
        result["prompt_tokens"] += int(usage.get("prompt_tokens", 0) or 0)
        result["completion_tokens"] += int(usage.get("completion_tokens", 0) or 0)
        result["total_tokens"] += int(usage.get("total_tokens", 0) or 0)
    return result


def _error_code(exc: Exception) -> str:
    if isinstance(exc, ModelAPIError):
        return exc.code
    if isinstance(exc, cc.ContentContractError):
        return "ANALYSIS_CONTRACT_INVALID"
    return "ANALYSIS_FAILED"


def _safe_message(exc: Exception) -> str:
    if isinstance(exc, ModelAPIError):
        return exc.message
    if isinstance(exc, cc.ContentContractError):
        return "模型输出未通过内容与证据校验。"
    return "文案分析失败。"


async def _thread(func: Callable[[], Any]) -> Any:
    import asyncio
    return await asyncio.to_thread(func)


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
