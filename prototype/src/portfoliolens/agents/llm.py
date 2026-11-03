"""OpenAI-compatible clients that return only parsed JSON objects."""

from __future__ import annotations

import json
import time
from typing import Any, Protocol

import httpx

from portfoliolens.config import LLMMode, Settings
from portfoliolens.errors import LLMError

RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


class LLMClient(Protocol):
    @property
    def name(self) -> str: ...

    def complete_json(self, system: str, user: str) -> dict[str, Any]: ...


class OpenAICompatibleClient:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        timeout_s: float,
        provider: str,
        extra_headers: dict[str, str] | None = None,
        max_retries: int = 3,
        http: httpx.Client | None = None,
    ) -> None:
        self._model = model
        self._url = f"{base_url}/chat/completions"
        self._timeout_s = timeout_s
        self._max_retries = max_retries
        self._http = http or httpx.Client()
        self._provider = provider
        self._headers = {"Authorization": f"Bearer {api_key}", **(extra_headers or {})}

    @property
    def name(self) -> str:
        return self._model

    def complete_json(self, system: str, user: str) -> dict[str, Any]:
        payload = {
            "model": self._model,
            "temperature": 0,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        last_error = "no attempt made"
        for attempt in range(self._max_retries):
            try:
                response = self._http.post(self._url, json=payload, headers=self._headers, timeout=self._timeout_s)
            except httpx.HTTPError as exc:
                last_error = f"transport error ({type(exc).__name__})"
            else:
                if response.status_code not in RETRYABLE_STATUS:
                    if response.is_error:
                        raise LLMError(f"{self._provider} returned HTTP {response.status_code}: {response.text[:200]}")
                    return parse_json_object(_message_content(response))
                last_error = f"HTTP {response.status_code}"
            if attempt + 1 < self._max_retries:
                time.sleep(min(2**attempt, 8))
        raise LLMError(f"{self._provider} request failed after {self._max_retries} attempts ({last_error})")


class OpenAIClient(OpenAICompatibleClient):
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        timeout_s: float,
        max_retries: int = 3,
        http: httpx.Client | None = None,
    ) -> None:
        super().__init__(
            api_key=api_key,
            model=model,
            base_url=base_url,
            timeout_s=timeout_s,
            provider="OpenAI",
            max_retries=max_retries,
            http=http,
        )


class OpenRouterClient(OpenAICompatibleClient):
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        timeout_s: float,
        max_retries: int = 3,
        http: httpx.Client | None = None,
    ) -> None:
        super().__init__(
            api_key=api_key,
            model=model,
            base_url=base_url,
            timeout_s=timeout_s,
            provider="OpenRouter",
            extra_headers={"X-Title": "PortfolioLens AI prototype"},
            max_retries=max_retries,
            http=http,
        )


def _message_content(response: httpx.Response) -> str:
    try:
        content = response.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise LLMError("Model response did not contain a message") from exc
    if not isinstance(content, str):
        raise LLMError("Model response content was not text")
    return content


def parse_json_object(text: str) -> dict[str, Any]:
    """Accept a bare JSON object or one wrapped in prose / code fences; reject everything else."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise LLMError("model output contained no JSON object")
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise LLMError(f"model output was not valid JSON ({exc.msg})") from exc
    if not isinstance(parsed, dict):
        raise LLMError("model output was not a JSON object")
    return parsed


def build_llm_client(settings: Settings) -> LLMClient | None:
    if settings.llm_mode is LLMMode.OFFLINE:
        return None
    if settings.llm_mode is LLMMode.OPENAI:
        assert settings.openai_api_key and settings.openai_model
        return OpenAIClient(
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            base_url=settings.openai_base_url,
            timeout_s=settings.llm_timeout_s,
        )
    assert settings.openrouter_api_key and settings.openrouter_model
    return OpenRouterClient(
        api_key=settings.openrouter_api_key,
        model=settings.openrouter_model,
        base_url=settings.openrouter_base_url,
        timeout_s=settings.llm_timeout_s,
    )
