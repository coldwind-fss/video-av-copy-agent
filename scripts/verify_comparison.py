from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.contracts import validate_output


def verify(base_url: str, comparison_id: str) -> dict[str, Any]:
    with httpx.Client(timeout=120) as client:
        response = client.get(f"{base_url}/api/comparisons/{comparison_id}")
        response.raise_for_status()
        data = response.json()
        manifest = data.get("manifest")
        if not manifest:
            raise AssertionError("comparison has no extracted frame manifest")
        expected = [{"frame_id": f["frame_id"], "timestamp_ms": f["timestamp_ms"]} for f in manifest["frames"]]
        hashes = [frame["image_sha256"] for frame in manifest["frames"]]
        branches = data["branches"]
        a, b = branches["A"], branches["B"]

        if a["status"] != "completed" or b["status"] != "completed":
            raise AssertionError(f"A/B statuses are {a['status']} / {b['status']}")
        if len(a["frames"]) != len(expected) or len(b["frames"]) != len(expected):
            raise AssertionError("A/B frame coverage does not match the manifest")
        if a["errors"] or not a["report"] or not b["report"]:
            raise AssertionError("a branch has frame errors or is missing its final report")
        if len(b["interval_observations"]) != max(0, len(expected) - 1):
            raise AssertionError("B interval observations do not cover all adjacent frame pairs")

        for frame, meta in zip(a["frames"], expected):
            validate_output("single_frame", {"schema_version": "1.1", "kind": "single_frame", "frame": frame}, frames=[meta])
        validate_output("final_report", a["report"], frames=expected, visual_intervals=[], target_brief=data.get("target_brief"))
        sequence = {
            "schema_version": "1.1", "kind": "sequence_analysis",
            "frames": b["frames"], "visual_intervals": b["interval_observations"],
            "temporal_segments": b.get("temporal_segments", []),
        }
        validate_output("sequence_analysis", sequence, frames=expected)
        validate_output(
            "final_report", b["report"], frames=expected,
            visual_intervals=b["interval_observations"], target_brief=data.get("target_brief"),
        )

        a_frame_calls = [call for call in a["calls"] if str(call.get("node", "")).startswith("f")]
        if len(a_frame_calls) != len(expected) or any(len(call.get("image_inputs", [])) != 1 for call in a_frame_calls):
            raise AssertionError("A calls do not contain exactly one image per frame")
        a_input_hashes = {
            call["node"]: call["image_inputs"][0].get("image_sha256") for call in a_frame_calls
        }
        if a_input_hashes != {frame["frame_id"]: frame["image_sha256"] for frame in manifest["frames"]}:
            raise AssertionError("A input image hashes do not match the shared manifest")

        sequence_calls = [call for call in b["calls"] if call.get("node") == "ordered-sequence"]
        if len(sequence_calls) != 1 or [item.get("image_sha256") for item in sequence_calls[0].get("image_inputs", [])] != hashes:
            raise AssertionError("B ordered image inputs do not match the shared manifest")
        if len(a["calls"]) != len(expected) + 1 or len(b["calls"]) != 2:
            raise AssertionError("logical call counts differ from A=N+1 / B=2")
        if manifest.get("tail_unobserved_ms") != max(0, manifest["duration_ms"] - manifest["frames"][-1]["timestamp_ms"]):
            raise AssertionError("tail coverage metadata is inconsistent with sampled timestamps")

        json_export = client.get(f"{base_url}/api/comparisons/{comparison_id}/export", params={"format": "json"})
        json_export.raise_for_status()
        exported = json_export.json()
        if exported["comparison"]["comparison_id"] != comparison_id:
            raise AssertionError("JSON export does not contain the current comparison")
        markdown_export = client.get(f"{base_url}/api/comparisons/{comparison_id}/export", params={"format": "md"})
        markdown_export.raise_for_status()
        if len(markdown_export.text) < 100:
            raise AssertionError("Markdown export is unexpectedly empty")

        frame_response = client.get(
            f"{base_url}/api/frame-sets/{manifest['frame_set_id']}/frames/f000"
        )
        frame_response.raise_for_status()
        video_response = client.get(
            f"{base_url}/api/videos/{data['video_id']}/content",
            headers={"Range": "bytes=0-1"},
        )
        if video_response.status_code not in {200, 206}:
            raise AssertionError(f"video content route returned HTTP {video_response.status_code}")

        return {
            "comparison_id": comparison_id,
            "status": data["status"],
            "video": (data.get("video_metadata") or {}).get("original_filename"),
            "duration_ms": manifest["duration_ms"],
            "sample_interval_ms": manifest["sample_interval_ms"],
            "frame_count": len(expected),
            "A_frames": len(a["frames"]),
            "B_frames": len(b["frames"]),
            "B_intervals": len(b["interval_observations"]),
            "B_temporal_segments": len(b["temporal_segments"]),
            "prompt_version": data["prompt_version"],
            "schema_version": data["schema_version"],
            "A_calls": len(a["calls"]),
            "B_calls": len(b["calls"]),
            "A_dedup_actions": sum(len(call.get("application_normalizations", [])) for call in a["calls"]),
            "total_tokens": data["usage"]["total_tokens"],
            "json_export": json_export.status_code,
            "markdown_export": markdown_export.status_code,
            "frame_image": frame_response.status_code,
            "video_content": video_response.status_code,
            "schema_and_evidence_checks": "passed",
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("comparison_id")
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    args = parser.parse_args()
    result = verify(args.base_url, args.comparison_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
