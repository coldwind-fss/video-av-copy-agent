from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .config import ROOT


SCHEMA_PATH = ROOT / "docs" / "schemas" / "content-analysis.schema.json"
SCHEMA: dict[str, Any] = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
PROMPTS_PATH = ROOT / "docs" / "content-analysis-v0.3" / "PROMPTS.md"
PROMPTS_TEXT = PROMPTS_PATH.read_text(encoding="utf-8")
WORKFLOW_VERSION = "content-v0.3"
PROMPT_VERSION = "content-prompts-v0.1"
TAXONOMY_VERSION = "content-tags-v0.1"

Draft202012Validator.check_schema(SCHEMA)
TRANSCRIPT_VALIDATOR = Draft202012Validator({"$schema": SCHEMA["$schema"], "$defs": SCHEMA["$defs"], **SCHEMA["$defs"]["Transcript"]})
REPORT_VALIDATOR = Draft202012Validator({"$schema": SCHEMA["$schema"], "$defs": SCHEMA["$defs"], **SCHEMA["$defs"]["Report"]})


class ContentContractError(Exception):
    def __init__(self, messages: list[str]):
        self.messages = messages
        super().__init__("; ".join(messages))


def prompt_section(marker: str) -> str:
    start = PROMPTS_TEXT.find(marker)
    if start < 0:
        raise RuntimeError(f"Prompt section not found: {marker}")
    end = PROMPTS_TEXT.find("\n## ", start + len(marker))
    section = PROMPTS_TEXT[start:] if end < 0 else PROMPTS_TEXT[start:end]
    first = section.find("```text")
    if first < 0:
        raise RuntimeError(f"Prompt text block not found: {marker}")
    begin = first + len("```text")
    finish = section.find("```", begin)
    if finish < 0:
        raise RuntimeError(f"Prompt text block is unfinished: {marker}")
    return section[begin:finish].strip()


COMMON_PROMPT = prompt_section("## 2. C00_COMMON")
VISUAL_PROMPT = prompt_section("## 3. C10_VISUAL")
COPY_PROMPT = prompt_section("## 4. C20_COPY_TAGS")
HOOK_PROMPT = prompt_section("## 5. C30_HOOK")
REPAIR_PROMPT = prompt_section("## 6. C90_REPAIR")


def contract(definitions: dict[str, Any], root_definition: dict[str, Any]) -> dict[str, Any]:
    return {"$schema": SCHEMA["$schema"], "$defs": definitions, **root_definition}


VISUAL_CONTRACT = contract(SCHEMA["$defs"], {
    "type": "object", "properties": {"observations": {"type": "array", "items": {
        "type": "object", "properties": {
            "frame_id": {"type": "string", "minLength": 1, "maxLength": 80},
            "description": {"type": "string", "minLength": 1, "maxLength": 80},
            "onscreen_text": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 1000}, "maxItems": 40},
            "limitations": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 1000}, "maxItems": 20},
        }, "required": ["frame_id", "description", "onscreen_text", "limitations"], "additionalProperties": False,
    }, "minItems": 1, "maxItems": 20}},
    "required": ["observations"], "additionalProperties": False,
})
COPY_TAGS_CONTRACT = contract(SCHEMA["$defs"], {
    "type": "object", "properties": {"copy": {
        "type": "object", "properties": {
            "status": {"enum": ["completed", "unavailable"]},
            "summary": {"$ref": "#/$defs/Claim"},
            "segments": {"type": "array", "items": {
                "type": "object", "properties": {
                    "segment_id": {"type": "string", "minLength": 1, "maxLength": 80},
                    "transcript_segment_ids": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 80}, "minItems": 1, "maxItems": 1000, "uniqueItems": True},
                    "roles": {"type": "array", "items": {"enum": ["pain_point", "audience", "product_intro", "selling_point", "proof", "scenario", "offer", "call_to_action", "other"]}, "minItems": 1, "maxItems": 9, "uniqueItems": True},
                    "purpose": {"allOf": [{"$ref": "#/$defs/Claim"}, {"properties": {"basis": {"const": "inferred"}}}]},
                }, "required": ["segment_id", "transcript_segment_ids", "roles", "purpose"], "additionalProperties": False,
            }, "maxItems": 1000},
        }, "required": ["status", "summary", "segments"], "additionalProperties": False,
        "allOf": [{
            "if": {"properties": {"status": {"const": "completed"}}},
            "then": {"properties": {"segments": {"minItems": 1}}},
            "else": {"properties": {
                "segments": {"maxItems": 0},
                "summary": {"properties": {"basis": {"const": "unknown"}}},
            }},
        }],
    }, "tags": {"$ref": "#/$defs/Tags"}},
    "required": ["copy", "tags"], "additionalProperties": False,
})
HOOK_CONTRACT = contract(SCHEMA["$defs"], {
    "type": "object", "properties": {
        "onscreen_text": {"$ref": "#/$defs/Claim"}, "visual_summary": {"$ref": "#/$defs/Claim"},
        "techniques": {"$ref": "#/$defs/Hook/properties/techniques"}, "explanation": {"$ref": "#/$defs/Claim"},
    }, "required": ["onscreen_text", "visual_summary", "techniques", "explanation"], "additionalProperties": False,
})
NODE_VALIDATORS = {
    "visual": Draft202012Validator(VISUAL_CONTRACT),
    "copy_tags": Draft202012Validator(COPY_TAGS_CONTRACT),
    "hook": Draft202012Validator(HOOK_CONTRACT),
}


def validate_transcript(value: Any) -> None:
    errors = [f"{list(e.path)}: {e.message}" for e in TRANSCRIPT_VALIDATOR.iter_errors(value)]
    errors.extend(transcript_semantic_errors(value))
    if errors:
        raise ContentContractError(errors[:20])


def validate_node(kind: str, value: Any) -> None:
    errors = [f"{list(e.path)}: {e.message}" for e in NODE_VALIDATORS[kind].iter_errors(value)]
    if errors:
        raise ContentContractError(errors[:20])


def transcript_semantic_errors(value: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    last_end = 0
    segment_ids: set[str] = set()
    word_ids: set[str] = set()
    duration = value.get("duration_ms")
    for segment in value.get("segments", []):
        if segment["segment_id"] in segment_ids:
            errors.append("transcript segment id is duplicated")
        segment_ids.add(segment["segment_id"])
        start, end = segment["start_ms"], segment["end_ms"]
        if start is not None:
            if not 0 <= start < end <= duration or start < last_end:
                errors.append("transcript segments must be ordered, non-overlapping, and inside video")
            last_end = end
        word_end = start or 0
        for word in segment["words"]:
            if word["word_id"] in word_ids:
                errors.append("transcript word id is duplicated")
            word_ids.add(word["word_id"])
            if start is None or end is None or not start <= word["start_ms"] < word["end_ms"] <= end or word["start_ms"] < word_end:
                errors.append("word timestamp is outside its segment or not ordered")
            word_end = word["end_ms"]
    if value.get("outcome") == "no_audio" and value.get("source") not in {"media_probe", "fixture"}:
        errors.append("no_audio can only come from media probing or an explicit fixture")
    if value.get("source") == "asr" and (not value.get("provider") or not value.get("model")):
        errors.append("ASR source requires the actual provider and model")
    if value.get("content_basis") != "speech" and value.get("timing") == "word":
        errors.append("word timing cannot be asserted for subtitle or unverified text")
    return errors


def report_semantic_errors(report: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    transcript = report.get("transcript")
    transcript_map = {x["segment_id"]: x for x in (transcript or {}).get("segments", [])}
    words = {w["word_id"]: w for x in transcript_map.values() for w in x["words"]}
    visuals = {x["observation_id"]: x for x in report.get("visual", {}).get("observations", [])}
    metadata = report.get("metadata", {})
    duration = report["duration_ms"]
    if transcript and (transcript["video_id"] != report["video_id"] or transcript["duration_ms"] != duration):
        errors.append("transcript belongs to a different video or duration")
    ids = set(transcript_map) | set(words) | set(visuals) | set(metadata) | ({transcript["transcript_id"]} if transcript else set())

    def check_claim(claim: dict[str, Any], location: str, *, hook: bool = False) -> None:
        basis = claim.get("basis")
        if basis == "unknown":
            if claim.get("value") is not None or not claim.get("note"):
                errors.append(f"{location}: unknown claim requires null value and explanation")
            if claim.get("evidence"):
                errors.append(f"{location}: unknown claim should not claim supporting evidence")
        else:
            if not claim.get("value") or not claim.get("evidence"):
                errors.append(f"{location}: supported claim requires a value and evidence")
        for evidence in claim.get("evidence", []):
            evidence_id = evidence["id"]
            if evidence_id not in ids:
                errors.append(f"{location}: evidence id {evidence_id} was not provided to the model")
                continue
            item = transcript_map.get(evidence_id) or words.get(evidence_id) or visuals.get(evidence_id)
            if evidence.get("type") == "metadata":
                if evidence_id not in metadata:
                    errors.append(f"{location}: metadata evidence is not present")
            elif evidence.get("type") == "transcript_result":
                if not transcript or evidence_id != transcript["transcript_id"]:
                    errors.append(f"{location}: transcript result evidence does not match")
            elif evidence.get("quote") is not None:
                source_text = ""
                if item:
                    if evidence.get("type") == "visual_observation":
                        source_text = " ".join([item.get("description", ""), *item.get("onscreen_text", [])])
                    else:
                        source_text = item.get("text", "")
                if evidence["quote"] not in source_text:
                    errors.append(f"{location}: quoted text is not an exact substring of source evidence")
            if hook and item:
                window_end = report["hook"]["window_end_ms"]
                if evidence.get("type") == "visual_observation":
                    if not 0 <= item["timestamp_ms"] < window_end:
                        errors.append(f"{location}: visual evidence is outside the opening window")
                elif evidence.get("type") in {"transcript_segment", "transcript_word"}:
                    if item.get("start_ms") is None or not 0 <= item["start_ms"] < item["end_ms"] <= window_end:
                        errors.append(f"{location}: transcript evidence is outside the opening window")

    def visit(value: Any, location: str, *, in_hook: bool = False) -> None:
        if isinstance(value, dict):
            if {"value", "basis", "evidence", "note"}.issubset(value):
                check_claim(value, location, hook=in_hook)
            else:
                for key, child in value.items():
                    visit(child, f"{location}.{key}", in_hook=in_hook)
        elif isinstance(value, list):
            for i, child in enumerate(value):
                visit(child, f"{location}[{i}]", in_hook=in_hook)

    visit(report, "report")
    visit(report.get("hook", {}), "hook", in_hook=True)
    if report["hook"]["window_end_ms"] != min(3000, duration):
        errors.append("opening analysis window must start at zero and end at min(3000,duration)")
    if transcript and transcript["content_basis"] != "speech" and report["hook"]["spoken_excerpt"]["basis"] != "unknown":
        errors.append("subtitle or unverified text cannot be presented as spoken excerpt")
    if report.get("input_mode") == "fixture" and not report["provenance"].get("is_fixture"):
        errors.append("fixture provenance cannot be removed")
    if report.get("input_mode") == "asr" and (not transcript or transcript["source"] != "asr"):
        errors.append("ASR input mode requires an ASR transcript")
    if report.get("input_mode") == "manual" and (not transcript or transcript["source"] != "manual"):
        errors.append("manual input mode requires a manual transcript")
    origin = report.get("tags", {}).get("origin", {}).get("value")
    if origin in {"clip", "original"} and metadata.get("origin_hint") != origin:
        errors.append("confirmed source label requires matching declared source metadata")
    copy = report.get("copy", {})
    if copy.get("status") == "completed" and transcript:
        referenced = [sid for segment in copy["segments"] for sid in segment["transcript_segment_ids"]]
        expected = [segment["segment_id"] for segment in transcript["segments"]]
        if referenced != expected:
            errors.append("copy structure must cover transcript segments once and in source order")
        for segment in copy["segments"]:
            source = [transcript_map[sid] for sid in segment["transcript_segment_ids"] if sid in transcript_map]
            if source and (segment["start_ms"], segment["end_ms"]) != (source[0]["start_ms"], source[-1]["end_ms"]):
                errors.append("copy segment time must be derived from its transcript references")
    if report.get("status") == "skipped" and (report["scope"] != "excluded" or copy.get("status") != "skipped" or report["hook"]["status"] != "skipped"):
        errors.append("skipped analysis requires an excluded scope and skipped copy/hook modules")
    if report.get("status") == "completed" and (copy.get("status") != "completed" or report["hook"]["status"] != "available"):
        errors.append("completed analysis requires all applicable copy/hook modules")
    return errors


def validate_report(report: Any) -> None:
    errors = [f"{list(e.path)}: {e.message}" for e in REPORT_VALIDATOR.iter_errors(report)]
    if isinstance(report, dict):
        errors.extend(report_semantic_errors(report))
    if errors:
        raise ContentContractError(errors[:30])


def sha256_json(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
