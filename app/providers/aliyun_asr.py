from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from ..config import Settings
from ..transcription import AudioAsset, TranscriptError, TranscriptionOptions


class AliyunDashScopeFiletransProvider:
    """DashScope Qwen-Audio asynchronous file transcription adapter.

    The selected filetrans model accepts a publicly reachable audio/video URL. The
    application resolves that URL before calling this adapter; the adapter never
    receives or exposes a client supplied path or credential.
    """

    provider_name = "aliyun_dashscope"
    # DashScope reports silent tracks as a failed filetrans task with this
    # provider code instead of returning an empty successful transcript.  The
    # application contract has an explicit no_speech outcome, so normalize
    # these known responses into that outcome.
    _NO_SPEECH_CODES = {
        "ASR_RESPONSE_HAVE_NO_WORDS",
        "ASR_NO_SPEECH",
        "NO_SPEECH",
        "NO_AUDIO",
    }

    def __init__(self, settings: Settings):
        self.settings = settings

    def capabilities(self) -> dict[str, Any]:
        credentials_ready = bool(self.settings.asr_api_key and self.settings.asr_model and self.settings.asr_base_url)
        media_ready = self.settings.asr_media_mode != "public_url" or bool(self.settings.asr_public_base_url)
        ready = credentials_ready and media_ready
        status = "ready" if ready else ("media_url_not_configured" if credentials_ready and not media_ready else "not_connected")
        return {
            "configured": ready,
            "provider": self.provider_name,
            "model": self.settings.asr_model or None,
            "accepted_formats": ["aac", "amr", "avi", "flac", "flv", "m4a", "mkv", "mov", "mp3", "mp4", "mpeg", "ogg", "opus", "wav", "webm", "wma", "wmv"],
            "input_modes": ["url"],
            "timestamp_granularity": "word" if self.settings.asr_enable_words else "sentence",
            "max_duration_ms": 12 * 60 * 60 * 1000,
            "max_bytes": 2 * 1024 * 1024 * 1024,
            "status": status,
        }

    async def transcribe(self, audio: AudioAsset, options: TranscriptionOptions) -> dict[str, Any]:
        if not self.settings.asr_api_key or not self.settings.asr_model or not self.settings.asr_base_url:
            raise TranscriptError("ASR_NOT_CONFIGURED", "阿里云 ASR 配置不完整。", 503)
        if not audio.public_url:
            raise TranscriptError("ASR_MEDIA_URL_UNAVAILABLE", "阿里云 ASR 需要可公网访问的音频或视频地址。", 503)

        parameters: dict[str, Any] = {"channel_id": [0]}
        language_hint = options.language_hint or self.settings.asr_language_hint
        if language_hint:
            parameters["language_hints"] = [language_hint]
        if options.vocabulary:
            parameters["vocabulary"] = {word: 5 for word in options.vocabulary[:50] if word.strip()}

        payload = {
            "model": self.settings.asr_model,
            "input": {"file_urls": [audio.public_url]},
            # Filetrans requires the parameters object even when only channel_id is used.
            "parameters": parameters,
        }
        endpoint = f"{self.settings.asr_base_url}/api/v1/services/audio/asr/transcription"
        headers = {
            "Authorization": f"Bearer {self.settings.asr_api_key}",
            "X-DashScope-Async": "enable",
        }
        timeout = httpx.Timeout(self.settings.asr_timeout_seconds, connect=20.0)
        started = time.perf_counter()
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            task_id, request_id, submit_record = await self._submit(client, endpoint, headers, payload)
            result, query_record = await self._poll(client, task_id, headers)
            transcription_url = self._result_url(result)
            if transcription_url:
                transcription_response = await client.get(transcription_url)
                if transcription_response.status_code >= 400:
                    raise TranscriptError("ASR_RESPONSE_INVALID", "阿里云返回的转写结果无法读取。", 502)
                try:
                    raw = transcription_response.json()
                except ValueError as exc:
                    raise TranscriptError("ASR_RESPONSE_INVALID", "阿里云返回的转写结果不是有效 JSON。", 502) from exc
            else:
                raw = result

        parsed = self._parse_transcription(raw)
        parsed.update({
            "provider": self.provider_name,
            "model": self.settings.asr_model,
            "language": language_hint or None,
            "remote_job_id": task_id,
            "request_id": request_id,
            "usage": result.get("usage") or {},
            "provider_record": {
                "request_id": request_id,
                "remote_job_id": task_id,
                "submit": submit_record,
                "query": query_record,
                "latency_ms": round((time.perf_counter() - started) * 1000),
            },
        })
        return parsed

    async def _submit(self, client: httpx.AsyncClient, endpoint: str, headers: dict[str, str], payload: dict[str, Any]) -> tuple[str, str | None, dict[str, Any]]:
        try:
            response = await client.post(endpoint, headers={**headers, "Content-Type": "application/json"}, json=payload)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise TranscriptError("ASR_CONNECTION_FAILED", "连接阿里云语音识别服务失败。", 503) from exc
        record = {"http_status": response.status_code}
        if response.status_code in (401, 403):
            raise TranscriptError("ASR_AUTH_FAILED", "阿里云 ASR 鉴权失败。", 502)
        if response.status_code == 429:
            raise TranscriptError("ASR_RATE_LIMITED", "阿里云 ASR 当前达到调用限额。", 429)
        if response.status_code >= 500:
            raise TranscriptError("ASR_PROVIDER_ERROR", "阿里云 ASR 服务暂时不可用。", 503)
        if response.status_code >= 400:
            raise TranscriptError("ASR_REQUEST_REJECTED", "阿里云 ASR 拒绝了转写请求。", 422)
        try:
            data = response.json()
        except ValueError as exc:
            raise TranscriptError("ASR_RESPONSE_INVALID", "阿里云 ASR 提交响应无法解析。", 502) from exc
        output = data.get("output") or {}
        task_id = output.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise TranscriptError("ASR_RESPONSE_INVALID", "阿里云 ASR 没有返回任务 ID。", 502)
        return task_id, data.get("request_id"), record

    async def _poll(self, client: httpx.AsyncClient, task_id: str, headers: dict[str, str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        endpoint = f"{self.settings.asr_base_url}/api/v1/tasks/{task_id}"
        deadline = time.monotonic() + self.settings.asr_timeout_seconds
        records: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            try:
                response = await client.get(endpoint, headers={"Authorization": headers["Authorization"]})
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                raise TranscriptError("ASR_CONNECTION_FAILED", "查询阿里云 ASR 任务失败。", 503) from exc
            records.append({"http_status": response.status_code})
            if response.status_code in (401, 403):
                raise TranscriptError("ASR_AUTH_FAILED", "阿里云 ASR 鉴权失败。", 502)
            if response.status_code >= 500:
                raise TranscriptError("ASR_PROVIDER_ERROR", "查询阿里云 ASR 任务失败。", 503)
            if response.status_code >= 400:
                raise TranscriptError("ASR_RESPONSE_INVALID", "阿里云 ASR 任务查询返回错误。", 502)
            try:
                data = response.json()
            except ValueError as exc:
                raise TranscriptError("ASR_RESPONSE_INVALID", "阿里云 ASR 任务响应无法解析。", 502) from exc
            output = data.get("output") or {}
            status = output.get("task_status")
            records[-1]["task_status"] = status
            if status in {"SUCCEEDED", "FAILED", "CANCELED", "UNKNOWN"}:
                if status != "SUCCEEDED":
                    message = self._subtask_message(output) or output.get("message") or "阿里云 ASR 任务未成功完成。"
                    code = self._subtask_code(output) or output.get("code") or "ASR_PROVIDER_FAILED"
                    records[-1]["code"] = code
                    if code in self._NO_SPEECH_CODES:
                        return {**output, "no_speech": True, "usage": data.get("usage") or {}}, records
                    raise TranscriptError(code, message, 502)
                return {**output, "usage": data.get("usage") or {}}, records
            await asyncio.sleep(max(1, self.settings.asr_poll_interval_seconds))
        raise TranscriptError("ASR_TIMEOUT", "阿里云 ASR 转写超时。", 504)

    @staticmethod
    def _result_url(output: dict[str, Any]) -> str | None:
        results = output.get("results") or []
        if not results:
            return None
        first = results[0] or {}
        if first.get("subtask_status") not in {None, "SUCCEEDED"}:
            raise TranscriptError(first.get("code") or "ASR_PROVIDER_FAILED", first.get("message") or "阿里云 ASR 子任务失败。", 502)
        url = first.get("transcription_url")
        return url if isinstance(url, str) and url else None

    @staticmethod
    def _subtask_code(output: dict[str, Any]) -> str | None:
        results = output.get("results") or []
        return (results[0] or {}).get("code") if results else None

    @staticmethod
    def _subtask_message(output: dict[str, Any]) -> str | None:
        results = output.get("results") or []
        return (results[0] or {}).get("message") if results else None

    @staticmethod
    def _parse_transcription(raw: dict[str, Any]) -> dict[str, Any]:
        transcripts = raw.get("transcripts") or []
        segments: list[dict[str, Any]] = []
        content_duration = 0
        for transcript in transcripts:
            content_duration = max(content_duration, int(transcript.get("content_duration_in_milliseconds") or 0))
            for sentence in transcript.get("sentences") or []:
                text = str(sentence.get("text") or "").strip()
                start = sentence.get("begin_time")
                end = sentence.get("end_time")
                if not text or not isinstance(start, int) or not isinstance(end, int):
                    continue
                words: list[dict[str, Any]] = []
                for word in sentence.get("words") or []:
                    word_text = str(word.get("text") or "").strip()
                    word_start, word_end = word.get("begin_time"), word.get("end_time")
                    if word_text and isinstance(word_start, int) and isinstance(word_end, int):
                        words.append({"text": word_text, "start_ms": word_start, "end_ms": word_end})
                segments.append({"text": text, "start_ms": start, "end_ms": end, "words": words})
        return {"segments": segments, "no_speech": bool(raw.get("no_speech") or (not segments and content_duration == 0))}
