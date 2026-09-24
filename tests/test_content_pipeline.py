from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from typing import Any

import imageio_ffmpeg
from fastapi.testclient import TestClient

from app import content_contracts
from app.config import settings as base_settings
from app.content_media import opening_sample_times
from app.audio import extract_audio, probe_audio
from app.model_client import ModelResponse
from app.transcription import TranscriptError, parse_import, standardize_result


class FakeModelClient:
    async def close(self) -> None:
        return None

    async def complete(self, system: str, user_content: list[dict[str, Any]], *, max_tokens: int,
                       input_text: str, call_context: dict[str, Any], **kwargs: Any) -> ModelResponse:
        node = call_context["node_id"]
        parsed = json.loads(input_text)
        if node == "visual":
            output = {"observations": [{
                "frame_id": frame["frame_id"], "description": "测试素材中出现蓝色纯色背景。",
                "onscreen_text": [], "limitations": ["此条目由工程测试替身生成。"],
            } for frame in parsed["frames"]]}
        elif node == "copy_tags":
            transcript = parsed["transcript"]
            segments = transcript["segments"]
            ids = [segment["segment_id"] for segment in segments]
            evidence_id = ids[0]
            quote = segments[0]["text"]
            output = {
                "copy": {
                    "status": "completed",
                    "summary": {"value": "口播介绍产品并说明利益点。", "basis": "inferred",
                                "evidence": [{"type": "transcript_segment", "id": evidence_id, "quote": quote}], "note": "工程验收模拟结果。"},
                    "segments": [{"segment_id": "c000", "transcript_segment_ids": ids,
                                  "roles": ["product_intro"], "start_ms": 3900, "end_ms": 3999,
                                  "purpose": {"value": "介绍产品。", "basis": "inferred",
                                              "evidence": [{"type": "transcript_segment", "id": evidence_id, "quote": quote}],
                                              "note": "工程验收模拟结果。"}}],
                    "structure_pattern": "模型提供的字段不会被采信。",
                },
                "tags": {"format_tags": [], "origin": {"value": None, "basis": "unknown", "evidence": [], "note": "无来源证据。"}, "topic_tags": []},
            }
        else:
            visual = parsed["visual_evidence"]
            first_visual = visual[0]
            excerpt_evidence = (parsed["transcript_evidence"] or
                                [{"type": "visual_observation", "id": first_visual["observation_id"], "quote": None}])[0]
            output = {
                "onscreen_text": {"value": None, "basis": "unknown", "evidence": [], "note": "测试画面没有已识别文字。"},
                "visual_summary": {"value": first_visual["description"], "basis": "observed",
                                   "evidence": [{"type": "visual_observation", "id": "vf000", "quote": None}], "note": "工程验收模拟结果。"},
                "techniques": [],
                "explanation": {"value": "开场口播直接介绍产品。", "basis": "inferred",
                                "evidence": [excerpt_evidence], "note": "工程验收模拟结果。"},
            }
        call_id = "call_" + uuid.uuid4().hex
        record = {"call_id": call_id, "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}
        return ModelResponse(json.dumps(output, ensure_ascii=False), "mock-acceptance-model", record)


class ContentPipelineAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory(prefix="frame-lab-content-acceptance-")
        cls.root = Path(cls.temp.name)
        cls.video_path = cls.root / "silent-test.mp4"
        command = [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                   "color=c=blue:s=320x240:d=4:r=4", "-an", "-c:v", "mpeg4", "-y", str(cls.video_path)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
        if result.returncode:
            raise RuntimeError(f"Cannot create acceptance video: {result.stderr[-400:]}")
        cls.audio_video_path = cls.root / "audio-test.mp4"
        audio_command = [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                         "color=c=blue:s=320x240:d=4:r=4", "-f", "lavfi", "-i",
                         "sine=frequency=440:sample_rate=44100:duration=4", "-shortest", "-c:v", "mpeg4", "-c:a", "aac", "-y", str(cls.audio_video_path)]
        result = subprocess.run(audio_command, capture_output=True, text=True, timeout=30, check=False)
        if result.returncode:
            raise RuntimeError(f"Cannot create audio acceptance video: {result.stderr[-400:]}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def setUp(self) -> None:
        from app import main
        self.main = main
        self.previous_settings = main.settings
        self.previous_runtime_settings = main.runtime.settings
        self.previous_client = main.runtime.client
        self.previous_active_job = main.runtime.active_job
        self.previous_data_env = os.environ.get("APP_DATA_DIR")
        os.environ["APP_DATA_DIR"] = str(self.root / f"data-{uuid.uuid4().hex}")
        test_settings = dataclasses.replace(base_settings, model_api_key="acceptance-only", app_read_only=False,
                                             asr_provider="disabled", ffprobe_path="")
        main.settings = test_settings
        main.runtime.settings = test_settings
        main.runtime.active_job = False
        self.client = TestClient(main.app)
        self.client.__enter__()
        self.original_lifespan_client = main.runtime.client
        main.runtime.client = FakeModelClient()

    def tearDown(self) -> None:
        self.main.runtime.client = self.original_lifespan_client
        self.client.__exit__(None, None, None)
        self.main.runtime.client = self.previous_client
        self.main.runtime.settings = self.previous_runtime_settings
        self.main.settings = self.previous_settings
        self.main.runtime.active_job = self.previous_active_job
        if self.previous_data_env is None:
            os.environ.pop("APP_DATA_DIR", None)
        else:
            os.environ["APP_DATA_DIR"] = self.previous_data_env

    def upload(self, path: Path | None = None) -> dict[str, Any]:
        path = path or self.video_path
        with path.open("rb") as video:
            response = self.client.post("/api/videos", files={"file": ("silent-test.mp4", video, "video/mp4")})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def wait_for(self, url: str, terminal: set[str], timeout: float = 20.0) -> dict[str, Any]:
        until = time.monotonic() + timeout
        last = {}
        while time.monotonic() < until:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, response.text)
            last = response.json()
            if last.get("status") in terminal:
                return last
            time.sleep(0.1)
        self.fail(f"Task did not finish: {last.get('status')}")

    def test_import_analysis_history_review_and_exports(self) -> None:
        video = self.upload()
        self.assertEqual(self.client.get(f"/api/videos/{video['video_id']}/metadata").status_code, 200)
        srt = "1\n00:00:00,200 --> 00:00:01,400\n春天通勤，轻装出发。\n"
        imported = self.client.post("/api/transcripts/import", json={
            "video_id": video["video_id"], "format": "srt", "content": srt,
            "content_basis": "speech", "reviewed_by_human": True,
        })
        self.assertEqual(imported.status_code, 201, imported.text)
        transcript = imported.json()["transcript"]
        content_contracts.validate_transcript(transcript)
        self.assertEqual(transcript["timing"], "sentence")
        self.assertTrue(transcript["reviewed_by_human"])

        created = self.client.post("/api/content-analyses", json={
            "video_id": video["video_id"], "transcript_id": imported.json()["transcript_id"],
            "scope_hint": "speech_content", "origin_hint": "unknown",
        })
        self.assertEqual(created.status_code, 202, created.text)
        analysis = self.wait_for(created.json()["status_url"], {"completed", "partial", "failed"})
        if analysis["status"] != "completed":
            self.fail(json.dumps(analysis, ensure_ascii=False, indent=2))
        self.assertEqual(analysis["status"], "completed", analysis.get("error"))
        report = analysis["report"]
        content_contracts.validate_report(report)
        self.assertEqual(report["input_mode"], "manual")
        self.assertEqual(report["hook"]["status"], "available")
        self.assertEqual(report["copy"]["structure_pattern"], "商品介绍")
        self.assertEqual(report["copy"]["segments"][0]["start_ms"], 200)
        copy_call = next(call for call in analysis["model_calls"] if call["node_id"] == "copy_tags")
        self.assertEqual(len(copy_call["application_normalizations"]), 3)
        self.assertEqual(len(report["visual"]["observations"]), 7)
        self.assertTrue(analysis["model_calls"])

        visual_claim = report["hook"]["visual_summary"]
        visual_claim["evidence"][0]["quote"] = report["visual"]["observations"][0]["description"][:8]
        content_contracts.validate_report(report)
        visual_claim["evidence"][0]["quote"] = "这段文字不在视觉证据中。"
        with self.assertRaises(content_contracts.ContentContractError):
            content_contracts.validate_report(report)

        frame_id = report["visual"]["observations"][0]["frame_id"]
        frame = self.client.get(f"/api/content-analyses/{analysis['analysis_id']}/frames/{frame_id}")
        self.assertEqual(frame.status_code, 200)
        self.assertEqual(frame.headers["content-type"], "image/jpeg")
        filtered = self.client.get("/api/content-analyses", params={"input_mode": "manual"}).json()
        self.assertEqual(filtered["total"], 1)
        self.assertEqual(filtered["items"][0]["analysis_id"], analysis["analysis_id"])

        review = self.client.put(f"/api/content-analyses/{analysis['analysis_id']}/review", json={
            "verdict": "usable", "dimensions": {"transcript_fidelity": 5, "hook_evidence": 4},
            "notes": "工程验收评价。", "reviewer": "acceptance",
        })
        self.assertEqual(review.status_code, 200, review.text)
        self.assertTrue(review.json()["saved"])
        self.assertEqual(self.client.get(f"/api/content-analyses/{analysis['analysis_id']}/export?format=json").status_code, 200)
        markdown = self.client.get(f"/api/content-analyses/{analysis['analysis_id']}/export?format=md")
        self.assertEqual(markdown.status_code, 200)
        self.assertIn("## 前三秒", markdown.text)
        self.assertIn("## 业务评价", markdown.text)

    def test_content_analysis_can_start_from_existing_visual_comparison(self) -> None:
        video = self.upload()
        comparison_id = "cmp_legacy_" + uuid.uuid4().hex
        comparison = {
            "comparison_id": comparison_id,
            "video_id": video["video_id"],
            "status": "completed",
            "manifest": {
                "duration_ms": 4000,
                "frames": [
                    {"frame_id": "f000", "timestamp_ms": 0},
                    {"frame_id": "f001", "timestamp_ms": 2000},
                ],
            },
            "branches": {
                "A": {"frames": [
                    {"frame_id": "f000", "visible_text": {"value": "新品轻量马甲", "basis": "observed"}},
                    {"frame_id": "f001", "visible_text": {"value": "新品轻量马甲", "basis": "observed"}},
                ]},
            },
        }
        self.main.atomic_write_json(self.main.runtime.comparison_path(comparison_id), comparison)

        created = self.client.post(f"/api/comparisons/{comparison_id}/content-analysis", json={
            "scope_hint": "speech_content", "origin_hint": "unknown",
        })
        self.assertEqual(created.status_code, 202, created.text)
        self.assertEqual(created.json()["content_basis"], "subtitle")
        analysis = self.wait_for(created.json()["status_url"], {"completed", "partial", "failed"})
        self.assertIn(analysis["status"], {"completed", "partial"}, analysis.get("error"))
        report = analysis["report"]
        content_contracts.validate_report(report)
        self.assertEqual(report["input_mode"], "manual")
        self.assertEqual(report["transcript"]["content_basis"], "subtitle")
        self.assertTrue(any("画面文字证据" in item for item in report["limitations"]))
        filtered = self.client.get("/api/content-analyses", params={"video_id": video["video_id"]}).json()
        self.assertEqual(filtered["total"], 1)
        by_comparison = self.client.get("/api/content-analyses", params={"comparison_id": comparison_id}).json()
        self.assertEqual(by_comparison["items"][0]["comparison_id"], comparison_id)

    def test_asr_unavailable_and_silent_track_detection(self) -> None:
        video = self.upload()
        self.assertFalse(self.client.get("/api/capabilities").json()["asr"]["configured"])
        created = self.client.post("/api/transcriptions", json={"video_id": video["video_id"]})
        self.assertEqual(created.status_code, 202, created.text)
        job = self.wait_for(created.json()["status_url"], {"no_audio", "waiting_provider", "failed"})
        self.assertEqual(job["status"], "no_audio", job.get("error"))
        transcript = self.client.get(f"/api/transcripts/{job['transcript_id']}").json()
        content_contracts.validate_transcript(transcript)
        self.assertEqual(transcript["outcome"], "no_audio")

    def test_audio_track_waits_for_provider_without_fabricating_text(self) -> None:
        video = self.upload(self.audio_video_path)
        probe = probe_audio(self.audio_video_path, self.main.settings)
        self.assertTrue(probe["has_audio"])
        extracted = extract_audio(self.audio_video_path, probe, self.root / "extracted" / "test.wav")
        import wave
        with wave.open(extracted["path"], "rb") as audio:
            self.assertEqual(audio.getframerate(), 16000)
            self.assertEqual(audio.getnchannels(), 1)
            self.assertGreater(audio.getnframes(), 0)
        created = self.client.post("/api/transcriptions", json={"video_id": video["video_id"]})
        self.assertEqual(created.status_code, 202, created.text)
        job = self.wait_for(created.json()["status_url"], {"no_audio", "waiting_provider", "failed"})
        self.assertEqual(job["status"], "waiting_provider", job.get("error"))
        self.assertTrue(job["probe"]["has_audio"])
        self.assertEqual(job["error"]["code"], "ASR_NOT_CONFIGURED")
        self.assertIsNone(job["transcript_id"])
        self.assertIsNone(job["audio_asset"])

    def test_import_validation_and_opening_sampling(self) -> None:
        valid = parse_import(format="json", content={"segments": [{"text": "A B", "start_ms": 100, "end_ms": 900,
                                    "words": [{"text": "A", "start_ms": 100, "end_ms": 300}, {"text": "B", "start_ms": 400, "end_ms": 700}]}]},
                             content_basis="speech", video_id="video_test", duration_ms=1000,
                             reviewed_by_human=False, input_sha256="a" * 64)
        content_contracts.validate_transcript(valid)
        self.assertEqual(valid["timing"], "word")
        fixture_settings = dataclasses.replace(base_settings, asr_provider="fixture", content_fixture_enabled=True)
        fixture = standardize_result(result={"provider": "fixture", "model": "fixture-v1", "language": "zh",
                                             "segments": [{"text": "模拟文本", "start_ms": 0, "end_ms": 500}]},
                                     settings=fixture_settings, video_id="video_test", duration_ms=1000,
                                     input_sha256="c" * 64, offset_from_video_ms=None)
        content_contracts.validate_transcript(fixture)
        self.assertEqual(fixture["source"], "fixture")
        self.assertTrue(fixture["is_fixture"])
        plain = parse_import(format="text", content="整段口播原文，没有时间戳。", content_basis="speech",
                             video_id="video_test", duration_ms=1000, reviewed_by_human=True, input_sha256="b" * 64)
        content_contracts.validate_transcript(plain)
        self.assertEqual(plain["timing"], "none")
        with self.assertRaises(TranscriptError):
            parse_import(format="srt", content="1\n00:00:00,800 --> 00:00:01,200\nlate\n\n2\n00:00:00,200 --> 00:00:00,500\nearly\n",
                         content_basis="subtitle", video_id="video_test", duration_ms=1500,
                         reviewed_by_human=False, input_sha256=None)
        times = opening_sample_times(10_000, 20)
        self.assertEqual(times[:6], [0, 500, 1000, 1500, 2000, 2500])
        self.assertLess(len(times), 20)
        self.assertLess(times[-1], 10_000)
        self.assertEqual(len(opening_sample_times(60_000, 20)), 20)

    def test_read_only_mode_blocks_all_new_writes(self) -> None:
        previous = self.main.settings
        readonly = dataclasses.replace(previous, app_read_only=True)
        self.main.settings = readonly
        self.main.runtime.settings = readonly
        try:
            self.assertTrue(self.client.get("/api/capabilities").json()["read_only"])
            with self.video_path.open("rb") as video:
                self.assertEqual(self.client.post("/api/videos", files={"file": ("test.mp4", video, "video/mp4")}).status_code, 403)
            self.assertEqual(self.client.post("/api/transcriptions", json={"video_id": "video_missing"}).status_code, 403)
            self.assertEqual(self.client.post("/api/transcripts/import", json={"video_id": "video_missing", "format": "text", "content": "x"}).status_code, 403)
            self.assertEqual(self.client.post("/api/content-analyses", json={"video_id": "video_missing"}).status_code, 403)
            self.assertEqual(self.client.put("/api/content-analyses/ca_missing/review", json={}).status_code, 403)
        finally:
            self.main.settings = previous
            self.main.runtime.settings = previous


if __name__ == "__main__":
    unittest.main()
