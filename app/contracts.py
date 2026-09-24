from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .config import ROOT


SCHEMA_PATH = ROOT / "docs" / "schemas" / "analysis.schema.json"
SCHEMA: dict[str, Any] = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
PROMPTS_PATH = ROOT / "docs" / "PROMPTS.md"
PROMPTS_TEXT = PROMPTS_PATH.read_text(encoding="utf-8")
PROMPT_VERSION = "poc-v0.2"
SCHEMA_VERSION = "1.1"


class ContractError(Exception):
    def __init__(self, messages: list[str]):
        self.messages = messages
        super().__init__("; ".join(messages))


def prompt_section(marker: str) -> str:
    start = PROMPTS_TEXT.find(marker)
    if start < 0:
        raise RuntimeError(f"Prompt section not found: {marker}")
    next_section = PROMPTS_TEXT.find("\n## ", start + len(marker))
    if next_section < 0:
        next_section = len(PROMPTS_TEXT)
    section = PROMPTS_TEXT[start:next_section]
    first_fence = section.find("```text")
    if first_fence < 0:
        raise RuntimeError(f"Prompt text block not found: {marker}")
    begin = first_fence + len("```text")
    end = section.find("```", begin)
    if end < 0:
        raise RuntimeError(f"Prompt text block is unfinished: {marker}")
    return section[begin:end].strip()


COMMON_PROMPT = prompt_section("## 2. P00_COMMON")
SINGLE_PROMPT = prompt_section("## 3. P10_SINGLE_FRAME")
SEQUENCE_PROMPT = prompt_section("## 4. P20_ORDERED_SEQUENCE")
SYNTHESIS_PROMPT = prompt_section("## 5. P30_SYNTHESIZE")
RETRY_PROMPT = prompt_section("## 6. P90_RETRY")


def example_data(name: str) -> dict[str, Any]:
    path = ROOT / "docs" / "examples" / name
    return json.loads(path.read_text(encoding="utf-8"))


def _contract(def_name: str) -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": copy.deepcopy(SCHEMA["$defs"]),
        **copy.deepcopy(SCHEMA["$defs"][def_name]),
    }


SINGLE_CONTRACT = _contract("SingleFrameOutput")
SEQUENCE_CONTRACT = _contract("SequenceOutput")
REPORT_CONTRACT = _contract("FinalReport")
VALIDATORS = {
    "single_frame": Draft202012Validator(SINGLE_CONTRACT),
    "sequence_analysis": Draft202012Validator(SEQUENCE_CONTRACT),
    "final_report": Draft202012Validator(REPORT_CONTRACT),
}
Draft202012Validator.check_schema(SCHEMA)


def _fields(item: dict[str, Any], fields: set[str]):
    for field in fields:
        yield field, item[field]


def _claim_errors(
    claim: Any,
    allowed_ids: set[str],
    location: str,
    *,
    exact_id: str | None = None,
    observed_allowed: bool = True,
) -> list[str]:
    if not isinstance(claim, dict):
        return [f"{location}: expected a Claim object"]
    errors: list[str] = []
    evidence = claim.get("evidence_frame_ids", [])
    if not set(evidence).issubset(allowed_ids):
        errors.append(f"{location}: evidence references a frame outside its input scope")
    if exact_id and set(evidence) - {exact_id}:
        errors.append(f"{location}: a frame observation may cite only its own frame")
    if not observed_allowed and claim.get("basis") == "observed":
        errors.append(f"{location}: text-only synthesis cannot label a visual change observed")
    return errors


FRAME_CLAIMS = {
    "subject", "scene", "shot_size", "camera_angle", "composition",
    "lighting", "color_palette", "visible_pose", "visible_text",
}
INTERVAL_CLAIMS = {"visual_change", "subject_motion", "camera_motion", "transition"}


def _frame_errors(frames: list[dict[str, Any]], expected: list[dict[str, Any]], location: str) -> list[str]:
    errors: list[str] = []
    expected_by_id = {f["frame_id"]: f for f in expected}
    actual_ids = [f.get("frame_id") for f in frames]
    expected_ids = [f["frame_id"] for f in expected]
    if actual_ids != expected_ids:
        errors.append(f"{location}: frame IDs must exactly match the input IDs in order")
    for i, frame in enumerate(frames):
        fid = frame.get("frame_id")
        source = expected_by_id.get(fid)
        if source and frame.get("timestamp_ms") != source.get("timestamp_ms"):
            errors.append(f"{location}[{i}]: timestamp_ms must match the manifest")
        for name, claim in _fields(frame, FRAME_CLAIMS & frame.keys()):
            errors.extend(_claim_errors(claim, {fid} if isinstance(fid, str) else set(), f"{location}[{i}].{name}", exact_id=fid))
    return errors


def _interval_errors(
    intervals: list[dict[str, Any]],
    frames: list[dict[str, Any]],
    location: str,
    *,
    expected_source: str | None = None,
    text_only: bool = False,
    upstream_intervals: list[dict[str, Any]] | None = None,
) -> list[str]:
    errors: list[str] = []
    expected_pairs = list(zip(frames, frames[1:]))
    if len(intervals) != len(expected_pairs):
        errors.append(f"{location}: expected exactly one interval for each adjacent frame pair")
    upstream = {
        (x.get("from_frame_id"), x.get("to_frame_id")): x
        for x in (upstream_intervals or [])
    }
    for index, interval in enumerate(intervals):
        if index >= len(expected_pairs):
            break
        first, second = expected_pairs[index]
        pair = (first.get("frame_id"), second.get("frame_id"))
        if (interval.get("from_frame_id"), interval.get("to_frame_id")) != pair:
            errors.append(f"{location}[{index}]: interval must refer to its adjacent manifest frames")
        if interval.get("start_ms") != first.get("timestamp_ms") or interval.get("end_ms") != second.get("timestamp_ms"):
            errors.append(f"{location}[{index}]: interval timestamps must match the adjacent frame manifest")
        if interval.get("start_ms", 0) >= interval.get("end_ms", 0):
            errors.append(f"{location}[{index}]: interval must have positive duration")
        if expected_source and interval.get("evidence_source") != expected_source:
            errors.append(f"{location}[{index}]: evidence_source does not match this workflow")
        for name, claim in _fields(interval, INTERVAL_CLAIMS & interval.keys()):
            errors.extend(_claim_errors(claim, set(pair), f"{location}[{index}].{name}", observed_allowed=not text_only))
        if text_only and interval.get("visual_change", {}).get("basis") == "observed":
            errors.append(f"{location}[{index}]: text-only visual changes must be inferred or unknown")
        if upstream_intervals is not None:
            source_interval = upstream.get(pair)
            if source_interval:
                for name in ("visual_change", "subject_motion", "camera_motion", "transition"):
                    out_claim = interval.get(name, {})
                    in_claim = source_interval.get(name, {})
                    if out_claim.get("basis") == "observed" and in_claim.get("basis") != "observed":
                        errors.append(f"{location}[{index}].{name}: synthesis cannot upgrade an inferred observation to observed")
    return errors


def _segment_errors(
    segments: list[dict[str, Any]], frames: list[dict[str, Any]], location: str,
) -> list[str]:
    errors: list[str] = []
    positions = {frame.get("frame_id"): index for index, frame in enumerate(frames)}
    next_expected = 0
    for index, segment in enumerate(segments):
        start_id = segment.get("start_frame_id")
        end_id = segment.get("end_frame_id")
        if start_id not in positions or end_id not in positions:
            errors.append(f"{location}[{index}]: segment boundaries must reference input frames")
            continue
        start_index = positions[start_id]
        end_index = positions[end_id]
        if start_index > end_index:
            errors.append(f"{location}[{index}]: segment start must not follow its end")
            continue
        if start_index != next_expected:
            errors.append(f"{location}[{index}]: segments must partition all frames in order without gaps or overlap")
        if segment.get("start_ms") != frames[start_index].get("timestamp_ms"):
            errors.append(f"{location}[{index}]: start_ms must match start_frame_id")
        if segment.get("end_ms") != frames[end_index].get("timestamp_ms"):
            errors.append(f"{location}[{index}]: end_ms must match end_frame_id")
        allowed_ids = {frame.get("frame_id") for frame in frames[start_index:end_index + 1]}
        for name in ("description", "action_progression", "camera_motion", "continuity"):
            errors.extend(_claim_errors(segment.get(name), allowed_ids, f"{location}[{index}].{name}", observed_allowed=name == "description"))
        next_expected = end_index + 1
    if next_expected != len(frames):
        errors.append(f"{location}: segments must cover every input frame exactly once")
    return errors


def validate_output(kind: str, value: Any, *, frames: list[dict[str, Any]],
                    visual_intervals: list[dict[str, Any]] | None = None,
                    target_brief: dict[str, Any] | None = None) -> None:
    if kind not in VALIDATORS:
        raise ContractError([f"Unknown output kind: {kind}"])
    errors: list[str] = []
    for error in VALIDATORS[kind].iter_errors(value):
        path = ".".join(str(part) for part in error.absolute_path) or "$"
        errors.append(f"{path}: {error.message}"[:500])
        if len(errors) >= 10:
            break
    if not errors and isinstance(value, dict):
        if kind == "single_frame":
            errors.extend(_frame_errors([value["frame"]], frames, "frame"))
        elif kind == "sequence_analysis":
            errors.extend(_frame_errors(value["frames"], frames, "frames"))
            errors.extend(_interval_errors(value["visual_intervals"], frames, "visual_intervals", expected_source="ordered_images"))
            errors.extend(_segment_errors(value["temporal_segments"], frames, "temporal_segments"))
        elif kind == "final_report":
            errors.extend(_interval_errors(
                value["intervals"], frames, "intervals",
                text_only=bool(visual_intervals == []),
                upstream_intervals=visual_intervals,
            ))
            known_ids = {f["frame_id"] for f in frames}
            summary = value["summary"]
            for name in ("theme", "visual_style", "content_structure"):
                errors.extend(_claim_errors(summary[name], known_ids, f"summary.{name}"))
            for index, claim in enumerate(summary["reusable_features"]):
                errors.extend(_claim_errors(claim, known_ids, f"summary.reusable_features[{index}]"))
            wants_adaptation = bool(target_brief and any(
                (str(v).strip() if not isinstance(v, list) else any(str(x).strip() for x in v))
                for v in target_brief.values()
            ))
            if wants_adaptation and not value["adapted_prompt"]:
                errors.append("adapted_prompt: target brief was provided but no adapted prompt was produced")
            if not wants_adaptation and value["adapted_prompt"] is not None:
                errors.append("adapted_prompt: must be null when no target brief was provided")
    if errors:
        raise ContractError(errors)
