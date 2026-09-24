from __future__ import annotations

import base64
import copy
import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any

from .config import Settings
from .contracts import (
    COMMON_PROMPT, PROMPT_VERSION, REPORT_CONTRACT, RETRY_PROMPT,
    SCHEMA_VERSION, SEQUENCE_CONTRACT, SINGLE_CONTRACT, SINGLE_PROMPT,
    SEQUENCE_PROMPT, SYNTHESIS_PROMPT, ContractError, example_data,
    validate_output,
)
from .media import atomic_write_json, read_json
from .model_client import DeepSeekClient, ModelAPIError, ModelResponse


class WorkflowError(Exception):
    def __init__(self, code: str, message: str, calls: list[dict[str, Any]] | None = None):
        self.code = code
        self.message = message
        self.calls = calls or []
        super().__init__(message)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _render(template: str, **values: str) -> str:
    for name, value in values.items():
        template = template.replace("{{" + name + "}}", value)
    return template


def _full_system(node_prompt: str, contract: dict[str, Any], example: dict[str, Any] | None = None) -> str:
    return _render(
        COMMON_PROMPT,
        OUTPUT_CONTRACT_JSON=json.dumps(contract, ensure_ascii=False, separators=(",", ":")),
        OUTPUT_EXAMPLE_JSON=json.dumps(example or {}, ensure_ascii=False, separators=(",", ":")),
    ) + "\n\n" + node_prompt


def _brief_present(brief: dict[str, Any] | None) -> bool:
    if not brief:
        return False
    return any(
        any(str(value).strip() for value in item) if isinstance(item, list)
        else bool(str(item or "").strip())
        for item in brief.values()
    )


def _provider_usage(record: dict[str, Any]) -> dict[str, int]:
    usage = record.get("usage") or {}
    details = usage.get("prompt_tokens_details") or {}
    return {
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
        "cached_tokens": int(details.get("cached_tokens") or usage.get("prompt_cache_hit_tokens") or 0),
    }


def _dedupe_evidence_ids(value: Any, path: str = "$", actions: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    if actions is None:
        actions = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if key == "evidence_frame_ids" and isinstance(child, list):
                unique = []
                seen = set()
                for item in child:
                    if isinstance(item, str) and item in seen:
                        continue
                    if isinstance(item, str):
                        seen.add(item)
                    unique.append(item)
                removed = len(child) - len(unique)
                if removed:
                    value[key] = unique
                    actions.append({"path": child_path, "action": "deduplicated_evidence_frame_ids", "removed_count": removed})
            else:
                _dedupe_evidence_ids(child, child_path, actions)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _dedupe_evidence_ids(child, f"{path}[{index}]", actions)
    return actions


async def _run_node(
    *, client: DeepSeekClient, settings: Settings, work_dir: Path,
    branch_id: str, node_id: str, node_prompt: str, contract: dict[str, Any],
    example_name: str, kind: str, frames: list[dict[str, Any]], user_context: dict[str, Any],
    images: list[dict[str, Any]], target_brief: dict[str, Any] | None = None,
    upstream_intervals: list[dict[str, Any]] | None = None, max_tokens: int = 2048,
) -> tuple[dict[str, Any], dict[str, Any]]:
    example = example_data(example_name)
    system = _full_system(node_prompt, contract, example)
    context_text = json.dumps(user_context, ensure_ascii=False, separators=(",", ":"))
    base_blocks: list[dict[str, Any]] = [{"type": "text", "text": context_text}]
    for image in images:
        base_blocks.append({"type": "text", "text": json.dumps({"frame_id": image["frame_id"], "timestamp_ms": image["timestamp_ms"]}, ensure_ascii=False)})
        raw = Path(image["disk_path"]).read_bytes()
        data_url = "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")
        base_blocks.append({"type": "image_url", "image_url": {"url": data_url, "detail": "high"}})

    template_hash = _sha(system)
    template_path = work_dir / "prompt-snapshots" / f"{branch_id}-{node_id}.json"
    atomic_write_json(template_path, {
        "prompt_version": PROMPT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "system_prompt": system,
        "user_context": user_context,
        "image_inputs": [{k: v for k, v in item.items() if k != "disk_path"} for item in images],
        "system_prompt_sha256": template_hash,
    })

    call_records: list[dict[str, Any]] = []
    validation_errors: list[str] = []
    network_attempts = 0
    schema_attempts = 2
    for schema_attempt in range(schema_attempts):
        if network_attempts >= 3:
            raise WorkflowError("MODEL_OUTPUT_INVALID", "模型返回内容连续未通过输出字段检查。", call_records)
        text_block = context_text
        if schema_attempt:
            repair = _render(RETRY_PROMPT, VALIDATION_ERRORS_JSON=json.dumps(validation_errors[:10], ensure_ascii=False))
            text_block += "\n\n" + repair
        blocks = copy.deepcopy(base_blocks)
        blocks[0]["text"] = text_block
        input_text_hash = _sha(text_block)
        call_context = {
            "branch": branch_id,
            "node": node_id,
            "prompt_version": PROMPT_VERSION,
            "schema_version": SCHEMA_VERSION,
            "system_prompt_sha256": template_hash,
            "input_text_sha256": input_text_hash,
            "image_inputs": [{k: v for k, v in item.items() if k != "disk_path"} for item in images],
        }
        try:
            response: ModelResponse = await client.complete(
                system, blocks, max_tokens=max_tokens, input_text=text_block, call_context=call_context,
                max_attempts=3 - network_attempts,
            )
            network_attempts += len(response.record.get("transport_attempts") or [])
            record = {
                **response.record,
                "request_model": settings.model_id,
                "schema_attempt": schema_attempt + 1,
                "raw_response": response.content,
            }
            call_records.append(record)
            if record.get("finish_reason") == "length":
                record["validation_errors"] = ["$: output was truncated because finish_reason=length"]
                call_path = work_dir / "model-calls" / branch_id / f"{node_id}-truncated-{record['call_id']}.json"
                atomic_write_json(call_path, record)
                raise WorkflowError(
                    "MODEL_OUTPUT_TRUNCATED",
                    "模型输出达到 token 上限，已保存截断响应；请调整节点输出上限后新建任务。",
                    call_records,
                )
            parsed = json.loads(response.content)
            if not isinstance(parsed, dict):
                raise ContractError(["The top-level JSON response must be an object."])
            normalizations = _dedupe_evidence_ids(parsed)
            if normalizations:
                record["application_normalizations"] = normalizations
            validate_output(kind, parsed, frames=frames, visual_intervals=upstream_intervals, target_brief=target_brief)
            directory = work_dir / "model-calls" / branch_id
            call_path = directory / f"{node_id}-schema-{schema_attempt + 1}-{record['call_id']}.json"
            atomic_write_json(call_path, record)
            return parsed, record
        except ModelAPIError as exc:
            network_attempts += len(exc.record.get("transport_attempts") or [])
            call_records.append({
                **exc.record,
                "call_id": str(uuid.uuid4()),
                "request_model": settings.model_id,
                "branch": branch_id,
                "node": node_id,
                "schema_attempt": schema_attempt + 1,
                "error_code": exc.code,
                "error_message": exc.message,
                "system_prompt_sha256": template_hash,
                "input_text_sha256": input_text_hash,
                "image_inputs": call_context["image_inputs"],
            })
            path = work_dir / "model-calls" / branch_id / f"{node_id}-error-{call_records[-1]['call_id']}.json"
            atomic_write_json(path, call_records[-1])
            raise WorkflowError(exc.code, exc.message, call_records) from exc
        except (json.JSONDecodeError, ContractError) as exc:
            if isinstance(exc, ContractError):
                validation_errors = exc.messages
            else:
                validation_errors = ["$: response was not a valid JSON object"]
            record["validation_errors"] = validation_errors
            path = work_dir / "model-calls" / branch_id / f"{node_id}-schema-{schema_attempt + 1}-{record['call_id']}.json"
            atomic_write_json(path, record)
            if schema_attempt == schema_attempts - 1:
                raise WorkflowError("MODEL_OUTPUT_INVALID", "模型返回内容连续两次未通过输出字段检查。", call_records) from exc
    raise WorkflowError("MODEL_OUTPUT_INVALID", "模型输出检查失败。", call_records)


def _input_frame(frame: dict[str, Any], settings: Settings, work_dir: Path) -> dict[str, Any]:
    return {
        **frame,
        "disk_path": str(settings.data_dir / Path(frame["image_path"])),
    }


def _report_frame_set(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"frame_id": f["frame_id"], "timestamp_ms": f["timestamp_ms"]} for f in manifest["frames"]]


def _as_interval_notes(intervals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in item.items() if k not in {"start_ms", "end_ms"}} for item in intervals]


async def _run_synthesis(
    *, client: DeepSeekClient, settings: Settings, work_dir: Path,
    branch_id: str, frame_set_id: str, manifest: dict[str, Any],
    frame_results: list[dict[str, Any]], visual_intervals: list[dict[str, Any]],
    target_brief: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    mode = "ordered_images" if branch_id == "B" else "frame_descriptions"
    report_frames = _report_frame_set(manifest)
    context = {
        "evidence_mode": mode,
        "duration_ms": manifest["duration_ms"],
        "sample_interval_ms": manifest["sample_interval_ms"],
        "timestamp_basis": manifest["timestamp_basis"],
        "manifest": report_frames,
        "frames": frame_results,
        "visual_intervals": _as_interval_notes(visual_intervals),
        "target_brief": target_brief if _brief_present(target_brief) else None,
        "known_gaps": [
            f"尚未采样的尾部时长约 {manifest.get('tail_unobserved_ms', 0)} ms。",
            "本模型输入仅包含视频画面，不包含音轨。",
        ],
    }
    return await _run_node(
        client=client, settings=settings, work_dir=work_dir, branch_id=branch_id,
        node_id="synthesis", node_prompt=SYNTHESIS_PROMPT, contract=REPORT_CONTRACT,
        example_name="report-a.example.json", kind="final_report", frames=report_frames,
        user_context=context, images=[], target_brief=target_brief,
        upstream_intervals=visual_intervals, max_tokens=8192,
    )


async def _persist_call_usage(state: dict[str, Any], branch_id: str, records: list[dict[str, Any]]) -> None:
    branch = state["branches"][branch_id]
    for record in records:
        if record in branch["calls"]:
            continue
        branch["calls"].append(record)
        usage = _provider_usage(record)
        for key, value in usage.items():
            branch["usage"][key] += value


def _start_branch(state: dict[str, Any], branch_id: str) -> None:
    branch = state["branches"][branch_id]
    branch.update({"status": "running", "stage": "visual_analysis", "error": None, "started_at": time.time()})
    state["status"] = "running"


def _finish_branch(state: dict[str, Any], branch_id: str, status: str, started: float) -> None:
    branch = state["branches"][branch_id]
    branch["status"] = status
    branch["stage"] = "completed" if status == "completed" else status
    branch["wall_ms"] = round((time.perf_counter() - started) * 1000)
    branch["finished_at"] = time.time()


async def run_branch_a(*, state: dict[str, Any], manifest: dict[str, Any],
                       settings: Settings, client: DeepSeekClient, work_dir: Path,
                       target_brief: dict[str, Any] | None, save) -> None:
    branch_id = "A"
    started = time.perf_counter()
    _start_branch(state, branch_id)
    state["branches"][branch_id]["progress"] = {"completed": 0, "total": len(manifest["frames"])}
    await save()
    semaphore = __import__("asyncio").Semaphore(settings.frame_concurrency)
    all_frames = _report_frame_set(manifest)
    frame_results: dict[str, dict[str, Any]] = {}
    frame_errors: list[dict[str, Any]] = []

    async def inspect_one(frame: dict[str, Any]) -> None:
        async with semaphore:
            fid = frame["frame_id"]
            frame_meta = {"frame_id": fid, "timestamp_ms": frame["timestamp_ms"]}
            context = {**frame_meta, "timestamp_basis": manifest["timestamp_basis"], "task": "描述下一张图片的可见特征。"}
            try:
                result, call = await _run_node(
                    client=client, settings=settings, work_dir=work_dir, branch_id=branch_id,
                    node_id=f"{fid}", node_prompt=SINGLE_PROMPT, contract=SINGLE_CONTRACT,
                    example_name="single-frame.example.json", kind="single_frame", frames=[frame_meta],
                    user_context=context, images=[_input_frame(frame, settings, work_dir)], max_tokens=2048,
                )
                frame_results[fid] = result["frame"]
                await _persist_call_usage(state, branch_id, [call])
            except WorkflowError as exc:
                await _persist_call_usage(state, branch_id, exc.calls)
                frame_errors.append({"frame_id": fid, "code": exc.code, "message": exc.message})
            except Exception:
                frame_errors.append({"frame_id": fid, "code": "FRAME_ANALYSIS_FAILED", "message": "该帧分析遇到未处理错误。"})
            finally:
                branch = state["branches"][branch_id]
                branch["frames"] = [frame_results[f["frame_id"]] for f in all_frames if f["frame_id"] in frame_results]
                branch["progress"]["completed"] += 1
                await save()

    import asyncio
    await asyncio.gather(*(inspect_one(frame) for frame in manifest["frames"]))
    branch = state["branches"][branch_id]
    if frame_errors:
        branch["errors"] = frame_errors
        _finish_branch(state, branch_id, "partial", started)
        await save()
        return

    branch["stage"] = "synthesis"
    await save()
    try:
        report, record = await _run_synthesis(
            client=client, settings=settings, work_dir=work_dir, branch_id=branch_id,
            frame_set_id=manifest["frame_set_id"], manifest=manifest,
            frame_results=branch["frames"], visual_intervals=[], target_brief=target_brief,
        )
        branch["report"] = report
        await _persist_call_usage(state, branch_id, [record])
        _finish_branch(state, branch_id, "completed", started)
    except WorkflowError as exc:
        await _persist_call_usage(state, branch_id, exc.calls)
        branch["error"] = {"code": exc.code, "message": exc.message}
        _finish_branch(state, branch_id, "failed", started)
    except Exception:
        branch["error"] = {"code": "SYNTHESIS_FAILED", "message": "全片汇总节点遇到未处理错误。"}
        _finish_branch(state, branch_id, "failed", started)
    await save()


async def run_branch_b(*, state: dict[str, Any], manifest: dict[str, Any],
                       settings: Settings, client: DeepSeekClient, work_dir: Path,
                       target_brief: dict[str, Any] | None, save) -> None:
    branch_id = "B"
    started = time.perf_counter()
    _start_branch(state, branch_id)
    branch = state["branches"][branch_id]
    branch["progress"] = {"completed": 0, "total": len(manifest["frames"])}
    await save()
    frame_meta = _report_frame_set(manifest)
    context = {
        "duration_ms": manifest["duration_ms"],
        "sample_interval_ms": manifest["sample_interval_ms"],
        "timestamp_basis": manifest["timestamp_basis"],
        "frames": frame_meta,
        "task": "按顺序整体分析全部图片，输出逐帧证据、相邻帧变化，并把完整序列分成少量连续内容片段。",
    }
    try:
        sequence, call = await _run_node(
            client=client, settings=settings, work_dir=work_dir, branch_id=branch_id,
            node_id="ordered-sequence", node_prompt=SEQUENCE_PROMPT, contract=SEQUENCE_CONTRACT,
            example_name="sequence.example.json", kind="sequence_analysis", frames=frame_meta,
            user_context=context,
            images=[_input_frame(frame, settings, work_dir) for frame in manifest["frames"]],
            max_tokens=16384,
        )
        branch["frames"] = sequence["frames"]
        branch["interval_observations"] = sequence["visual_intervals"]
        branch["temporal_segments"] = sequence["temporal_segments"]
        branch["progress"]["completed"] = len(manifest["frames"])
        await _persist_call_usage(state, branch_id, [call])
        await save()
    except WorkflowError as exc:
        await _persist_call_usage(state, branch_id, exc.calls)
        branch["error"] = {"code": exc.code, "message": exc.message}
        _finish_branch(state, branch_id, "failed", started)
        await save()
        return
    except Exception:
        branch["error"] = {"code": "SEQUENCE_ANALYSIS_FAILED", "message": "有序多帧分析遇到未处理错误。"}
        _finish_branch(state, branch_id, "failed", started)
        await save()
        return

    branch["stage"] = "synthesis"
    await save()
    try:
        report, record = await _run_synthesis(
            client=client, settings=settings, work_dir=work_dir, branch_id=branch_id,
            frame_set_id=manifest["frame_set_id"], manifest=manifest,
            frame_results=branch["frames"], visual_intervals=branch["interval_observations"],
            target_brief=target_brief,
        )
        branch["report"] = report
        await _persist_call_usage(state, branch_id, [record])
        _finish_branch(state, branch_id, "completed", started)
    except WorkflowError as exc:
        await _persist_call_usage(state, branch_id, exc.calls)
        branch["error"] = {"code": exc.code, "message": exc.message}
        _finish_branch(state, branch_id, "failed", started)
    except Exception:
        branch["error"] = {"code": "SYNTHESIS_FAILED", "message": "全片汇总节点遇到未处理错误。"}
        _finish_branch(state, branch_id, "failed", started)
    await save()


def new_comparison_state(comparison_id: str, video_id: str, target_brief: dict[str, Any] | None,
                         interval_ms: int, model_id: str) -> dict[str, Any]:
    now = time.time()
    return {
        "comparison_id": comparison_id,
        "video_id": video_id,
        "status": "queued",
        "stage": "queued",
        "created_at": now,
        "finished_at": None,
        "error": None,
        "interval_ms": interval_ms,
        "target_brief": target_brief,
        "model_id": model_id,
        "prompt_version": PROMPT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "run_order": ["A", "B"],
        "manifest": None,
        "branches": {
            key: {
                "status": "pending",
                "stage": "pending",
                "progress": {"completed": 0, "total": 0},
                "frames": [],
                "interval_observations": [],
                "temporal_segments": [],
                "report": None,
                "calls": [],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cached_tokens": 0},
                "wall_ms": None,
                "errors": [],
                "error": None,
            } for key in ("A", "B")
        },
    }


async def execute_comparison(state: dict[str, Any], *, settings: Settings,
                             client: DeepSeekClient, save) -> None:
    comparison_started = time.perf_counter()
    comparison_id = state["comparison_id"]
    source_metadata = read_json(settings.videos_dir / state["video_id"] / "metadata.json")
    video_path = settings.videos_dir / state["video_id"] / source_metadata["storage_name"]
    work_dir = settings.comparisons_dir / comparison_id
    state["status"] = "extracting"
    state["stage"] = "extracting"
    await save()
    try:
        from starlette.concurrency import run_in_threadpool
        from .media import extract_frames, probe_video
        metadata = await run_in_threadpool(probe_video, video_path)
        extraction_started = time.perf_counter()
        manifest = await run_in_threadpool(
            extract_frames, video_path,
            "fs_" + uuid.uuid4().hex,
            state["interval_ms"], metadata, settings,
        )
        state["decode_ms"] = round((time.perf_counter() - extraction_started) * 1000)
        source = source_metadata
        manifest.update({
            "video_id": state["video_id"],
            "video_sha256": source["sha256"],
            "width": metadata["width"],
            "height": metadata["height"],
            "rotation": metadata["rotation"],
        })
        atomic_write_json(settings.frames_dir / manifest["frame_set_id"] / "manifest.json", manifest)
        state["manifest"] = manifest
        atomic_write_json(work_dir / "manifest.json", manifest)
        state["status"] = "running"
        state["stage"] = "A"
        await save()

        await run_branch_a(
            state=state, manifest=manifest, settings=settings, client=client, work_dir=work_dir,
            target_brief=state["target_brief"], save=save,
        )
        state["stage"] = "B"
        await save()
        await run_branch_b(
            state=state, manifest=manifest, settings=settings, client=client, work_dir=work_dir,
            target_brief=state["target_brief"], save=save,
        )
        statuses = [state["branches"][key]["status"] for key in ("A", "B")]
        state["status"] = "completed" if all(s == "completed" for s in statuses) else ("incomplete" if any(s in {"completed", "partial"} for s in statuses) else "failed")
        state["stage"] = "completed" if state["status"] == "completed" else state["status"]
        state["finished_at"] = time.time()
        state["comparison_wall_ms"] = round((time.perf_counter() - comparison_started) * 1000)
        await save()
    except Exception as exc:
        state["status"] = "failed"
        state["stage"] = "failed"
        failure = {"code": getattr(exc, "code", "COMPARISON_FAILED"), "message": getattr(exc, "message", "视频处理或分析失败。")}
        state["error"] = failure
        for branch in state["branches"].values():
            if branch["status"] in {"pending", "running"}:
                branch["status"] = "failed"
                branch["stage"] = "failed"
                branch["error"] = {"code": failure["code"], "message": "对照任务在该分支完成前失败。"}
        state["finished_at"] = time.time()
        state["comparison_wall_ms"] = round((time.perf_counter() - comparison_started) * 1000)
        await save()


def total_usage(state: dict[str, Any]) -> dict[str, int]:
    keys = ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens")
    return {key: sum(int(state["branches"][branch]["usage"].get(key, 0)) for branch in ("A", "B")) for key in keys}
