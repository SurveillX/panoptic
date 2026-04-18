"""
vLLM client — shared library.

Calls a vLLM server via its OpenAI-compatible /v1/chat/completions endpoint.
Supports multimodal requests (image_url content items) when frame URIs are
provided; falls back to plain text content for metadata-only summaries.

Retry policy:
  No internal retries.  Infrastructure failures (network, auth, timeout) are
  raised immediately so the executor's two-attempt loop can decide whether to
  repair (validation failure) or propagate (infrastructure failure).

Configuration (environment variables):
  VLLM_BASE_URL     — base URL of the vLLM server  (default: http://localhost:8000)
  VLLM_TOKEN        — bearer token; empty string = no Authorization header sent
  VLLM_MODEL        — model name in the request     (default: gemma-4-26b-it)
  VLLM_TIMEOUT_SEC  — per-request timeout in seconds (default: 60)
  VLLM_MAX_TOKENS   — max tokens in the response     (default: 1024)
  VLLM_TEMPERATURE  — sampling temperature           (default: 0.0)
"""

from __future__ import annotations

import json
import logging
import os

import httpx

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

VLLM_BASE_URL:    str   = os.environ.get("VLLM_BASE_URL", "http://localhost:8000")
VLLM_TOKEN:       str   = os.environ.get("VLLM_TOKEN", "")
VLLM_MODEL:       str   = os.environ.get("VLLM_MODEL", "gemma-4-26b-it")
VLLM_TIMEOUT_SEC: float = float(os.environ.get("VLLM_TIMEOUT_SEC", "60"))
VLLM_MAX_TOKENS:  int   = int(os.environ.get("VLLM_MAX_TOKENS", "1024"))
VLLM_TEMPERATURE: float = float(os.environ.get("VLLM_TEMPERATURE", "0.0"))


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class VLMNetworkError(IOError):
    """Network or transport error reaching the vLLM server."""


class VLMAuthError(PermissionError):
    """401 or 403 response from the vLLM server."""


class VLMError(RuntimeError):
    """Non-2xx response other than auth failures."""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class VLMClient:
    """
    HTTP client for the vLLM OpenAI-compatible API.

    Thread-safe: httpx.Client uses a connection pool internally.
    Create one instance per service process via get_vlm_client().
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        model: str,
        timeout_sec: float,
        max_tokens: int,
        temperature: float,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout_sec
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._headers = {"Content-Type": "application/json"}
        if token:
            self._headers["Authorization"] = f"Bearer {token}"

    def call(self, prompt_text: str, frame_uris: list[str], *, system_message: str | None = None) -> str:
        """
        Call /v1/chat/completions and return the raw text of the first choice.

        When frame_uris is non-empty, the message content is a list:
          [{"type": "text", "text": prompt_text},
           {"type": "image_url", "image_url": {"url": uri}}, ...]

        When frame_uris is empty, the message content is a plain string.
        This matches vLLM's OpenAI-compatible multimodal API.

        Raises
        ------
        VLMNetworkError  — httpx transport or timeout failure
        VLMAuthError     — 401 / 403
        VLMError         — other non-2xx response
        """
        if frame_uris:
            content: list | str = [{"type": "text", "text": prompt_text}] + [
                {"type": "image_url", "image_url": {"url": uri}}
                for uri in frame_uris
            ]
        else:
            content = prompt_text

        messages: list[dict] = []
        if system_message:
            messages.append({"role": "system", "content": system_message})
        messages.append({"role": "user", "content": content})

        body = {
            "model":           self._model,
            "messages":        messages,
            "temperature":     self._temperature,
            "max_tokens":      self._max_tokens,
            "response_format": {"type": "json_object"},
        }

        url = f"{self._base_url}/v1/chat/completions"

        try:
            response = httpx.post(
                url,
                json=body,
                headers=self._headers,
                timeout=self._timeout,
            )
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            raise VLMNetworkError(f"vLLM unreachable: {exc}") from exc

        if response.status_code in (401, 403):
            raise VLMAuthError(
                f"vLLM auth failure: {response.status_code} {url}"
            )

        if not response.is_success:
            raise VLMError(
                f"vLLM error: {response.status_code} {url} — {response.text[:200]}"
            )

        data = response.json()
        text_out: str = data["choices"][0]["message"]["content"]
        log.debug(
            "vlm call model=%s frames=%d tokens_approx=%d",
            self._model, len(frame_uris), len(text_out) // 4,
        )
        return text_out


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_vlm_client() -> VLMClient:
    """
    Return a VLMClient configured from environment variables.

      VLLM_BASE_URL     — base URL of the vLLM server
      VLLM_TOKEN        — bearer token (empty = no auth header)
      VLLM_MODEL        — model name passed in the request
      VLLM_TIMEOUT_SEC  — per-request timeout in seconds
      VLLM_MAX_TOKENS   — max tokens in the response
      VLLM_TEMPERATURE  — sampling temperature (0.0 = deterministic)
    """
    return VLMClient(
        base_url=VLLM_BASE_URL,
        token=VLLM_TOKEN,
        model=VLLM_MODEL,
        timeout_sec=VLLM_TIMEOUT_SEC,
        max_tokens=VLLM_MAX_TOKENS,
        temperature=VLLM_TEMPERATURE,
    )
