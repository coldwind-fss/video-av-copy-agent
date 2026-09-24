from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Settings


class ModelAPIError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False, record: dict[str, Any] | None = None):
        self.code = code
        self.message = message
        self.retryable = retryable
        self.record = record or {}
        super().__init__(message)


@dataclass
class ModelResponse:
    content: str
    model_id: str
    record: dict[str, Any]


class DeepSeekClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        timeout = httpx.Timeout(settings.model_timeout_seconds, connect=20.0)
        self.client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self.client.aclose()

    async def complete(self, system: str, user_content: list[dict[str, Any]], *,
                       max_tokens: int, input_text: str,
                       call_context: dict[str, Any], max_attempts: int = 3) -> ModelResponse:
        if not self.settings.model_api_key:
            raise ModelAPIError("MODEL_NOT_CONFIGURED", "未配置模型 API 密钥。")

        transport: list[dict[str, Any]] = []
        payload = {
            "model": self.settings.model_id,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "temperature": 0.2,
            "max_tokens": max_tokens,
            "stream": False,
        }

        delays = (0, 2, 5)[:max(1, min(3, max_attempts))]
        for attempt_index, delay in enumerate(delays):
            if delay:
                await asyncio.sleep(delay)
            started = time.perf_counter()
            try:
                response = await self.client.post(
                    f"{self.settings.model_base_url}/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {self.settings.model_api_key}"},
                )
                latency = round((time.perf_counter() - started) * 1000)
                transport_record: dict[str, Any] = {
                    "attempt": attempt_index + 1,
                    "http_status": response.status_code,
                    "latency_ms": latency,
                }
                if response.status_code in (429, 500, 502, 503, 504):
                    retry_after = response.headers.get("retry-after")
                    transport_record["retry_after_seconds"] = retry_after
                    transport.append(transport_record)
                    if attempt_index < len(delays) - 1:
                        if retry_after:
                            try:
                                await asyncio.sleep(min(float(retry_after), 10))
                            except ValueError:
                                pass
                        continue
                    status = response.status_code
                    raise ModelAPIError(
                        "MODEL_PROVIDER_BUSY" if status == 429 else "MODEL_PROVIDER_ERROR",
                        f"模型接口连续返回 HTTP {status}，本次工作流已停止。",
                        retryable=True,
                        record={"transport_attempts": transport, **call_context},
                    )
                if response.status_code >= 400:
                    transport.append(transport_record)
                    code = "MODEL_AUTH_FAILED" if response.status_code in (401, 403) else "MODEL_REQUEST_REJECTED"
                    raise ModelAPIError(code, f"模型接口返回 HTTP {response.status_code}。", record={"transport_attempts": transport, **call_context})

                data = response.json()
                transport.append(transport_record)
                choice = (data.get("choices") or [{}])[0]
                message = choice.get("message") or {}
                content = message.get("content")
                if not isinstance(content, str) or not content.strip():
                    record = self._record(data, choice, transport, call_context, len(input_text))
                    raise ModelAPIError("MODEL_EMPTY_RESPONSE", "模型返回空内容。", retryable=False, record=record)
                record = self._record(data, choice, transport, call_context, len(input_text))
                return ModelResponse(content, str(data.get("model") or self.settings.model_id), record)
            except ModelAPIError:
                raise
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                latency = round((time.perf_counter() - started) * 1000)
                transport.append({"attempt": attempt_index + 1, "error_type": type(exc).__name__, "latency_ms": latency})
                if attempt_index == len(delays) - 1:
                    raise ModelAPIError(
                        "MODEL_CONNECTION_FAILED", "连接模型接口失败，已达到本次重试上限。", retryable=True,
                        record={"transport_attempts": transport, **call_context},
                    ) from exc
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise ModelAPIError("MODEL_RESPONSE_INVALID", "模型接口返回了无法读取的响应。", record={"transport_attempts": transport, **call_context}) from exc
        raise ModelAPIError("MODEL_CONNECTION_FAILED", "连接模型接口失败。", retryable=True, record={"transport_attempts": transport, **call_context})

    @staticmethod
    def _record(data: dict[str, Any], choice: dict[str, Any], transport: list[dict[str, Any]],
                context: dict[str, Any], input_text_chars: int) -> dict[str, Any]:
        usage = data.get("usage") or {}
        return {
            "call_id": str(uuid.uuid4()),
            "request_id": data.get("id"),
            "returned_model": data.get("model"),
            "finish_reason": choice.get("finish_reason"),
            "usage": usage,
            "transport_attempts": transport,
            "input_text_chars": input_text_chars,
            **context,
        }
