import json
import logging
import time
from typing import Any

import httpx

from app.core.config import Settings
from app.core.exceptions import LLMServiceError
from app.llm.models import ModelTier
from app.llm.ports import LLMToolCall, LLMToolTurn

logger = logging.getLogger("app.llm")


class MantleLLMGateway:
    """Bedrock Mantle adapter for OpenAI-compatible GPT OSS and GPT models."""

    def __init__(self, settings: Settings, *, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=45.0)

    def complete(
        self,
        tier: ModelTier,
        *,
        system: str,
        user: str,
        json_mode: bool = False,
    ) -> str:
        if not self.settings.bedrock_api_key:
            raise LLMServiceError("BEDROCK_API_KEY is not configured")
        return self._openai_completion(tier, system=system, user=user, json_mode=json_mode)

    def complete_with_tools(
        self,
        tier: ModelTier,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> LLMToolTurn:
        """OpenAI-compatible native tool calling (`tools` + `tool_calls`)."""
        if not self.settings.bedrock_api_key:
            raise LLMServiceError("BEDROCK_API_KEY is not configured")
        model, max_tokens = self._model_and_budget(tier)
        body: dict[str, object] = {
            "model": model,
            "messages": [{"role": "system", "content": system}, *messages],
            "tools": tools,
            "tool_choice": "auto",
            "max_tokens": max_tokens,
        }
        if tier == "complex":
            body["reasoning_effort"] = self.settings.complex_reasoning_effort
        if tier == "router":
            body["reasoning_effort"] = self.settings.router_reasoning_effort
        response = self._post(
            f"{self.settings.bedrock_openai_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {self.settings.bedrock_api_key}"},
            json=body,
        )
        try:
            payload = response.json()
            message = payload["choices"][0]["message"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LLMServiceError("Bedrock Mantle returned an invalid Chat Completions response") from exc
        calls: list[LLMToolCall] = []
        for index, item in enumerate(message.get("tool_calls") or []):
            function = item.get("function") or {}
            raw_arguments = function.get("arguments") or "{}"
            try:
                arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else dict(raw_arguments)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Tool call arguments were not valid JSON for {function.get('name')}") from exc
            if not isinstance(arguments, dict):
                raise ValueError("Tool call arguments must be a JSON object")
            calls.append(LLMToolCall(str(item.get("id") or f"call_{index}"), str(function.get("name", "")), arguments))
        content = message.get("content")
        usage = payload.get("usage", {})
        logger.info(
            "llm_call_completed",
            extra={
                "model": model,
                "tier": tier,
                "input_tokens": usage.get("prompt_tokens"),
                "output_tokens": usage.get("completion_tokens"),
                "status": response.status_code,
                "tool_calls": len(calls),
            },
        )
        return LLMToolTurn(content.strip() if isinstance(content, str) and content.strip() else None, calls)

    def _model_and_budget(self, tier: ModelTier) -> tuple[str, int]:
        model = {
            "router": self.settings.router_model_id,
            "standard": self.settings.standard_model_id,
            "complex": self.settings.complex_model_id,
        }[tier]
        max_tokens = {
            "router": self.settings.router_max_output_tokens,
            "standard": self.settings.standard_max_output_tokens,
            "complex": self.settings.complex_max_output_tokens,
        }[tier]
        return model, max_tokens

    def _openai_completion(
        self,
        tier: ModelTier,
        *,
        system: str,
        user: str,
        json_mode: bool,
    ) -> str:
        model = {
            "router": self.settings.router_model_id,
            "standard": self.settings.standard_model_id,
            "complex": self.settings.complex_model_id,
        }[tier]
        max_tokens = {
            "router": self.settings.router_max_output_tokens,
            "standard": self.settings.standard_max_output_tokens,
            "complex": self.settings.complex_max_output_tokens,
        }[tier]
        body: dict[str, object] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        body["max_tokens"] = max_tokens
        if tier == "complex":
            body["reasoning_effort"] = self.settings.complex_reasoning_effort
        if tier == "router":
            # Routing needs a short structured decision, not extended hidden reasoning.
            # Keeping effort low avoids exhausting the output budget before JSON is emitted.
            body["reasoning_effort"] = self.settings.router_reasoning_effort
        # GPT OSS structured output is validated in the application. Mantle's GPT OSS
        # model card does not advertise strict structured-output support on this endpoint.
        response = self._post(
            f"{self.settings.bedrock_openai_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {self.settings.bedrock_api_key}"},
            json=body,
        )
        try:
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LLMServiceError("Bedrock Mantle returned an invalid Chat Completions response") from exc
        if not isinstance(content, str) or not content.strip():
            raise LLMServiceError("Bedrock Mantle returned an empty response")
        usage = payload.get("usage", {})
        logger.info(
            "llm_call_completed",
            extra={
                "model": model,
                "tier": tier,
                "input_tokens": usage.get("prompt_tokens"),
                "output_tokens": usage.get("completion_tokens"),
                "status": response.status_code,
            },
        )
        return content.strip()

    def _post(self, url: str, *, headers: dict[str, str], json: dict[str, object]) -> httpx.Response:
        request_headers = {"Content-Type": "application/json", **headers}
        attempts = self.settings.llm_max_retries + 1
        for attempt in range(attempts):
            try:
                response = self.client.post(url, headers=request_headers, json=json)
                if response.status_code < 400:
                    return response
                if response.status_code not in {408, 429} and response.status_code < 500:
                    raise LLMServiceError(
                        f"Bedrock Mantle rejected the request with status {response.status_code}"
                    )
            except httpx.RequestError as exc:
                if attempt == attempts - 1:
                    raise LLMServiceError("Bedrock Mantle could not be reached") from exc
            if attempt < attempts - 1:
                time.sleep(0.25 * (attempt + 1))
        raise LLMServiceError("Bedrock Mantle is temporarily unavailable")
