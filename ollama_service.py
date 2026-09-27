"""Bounded calls to the user's local Ollama installation."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

BASE_URL = os.environ.get("WITCHCRAFT_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
DEFAULT_MODEL = os.environ.get("WITCHCRAFT_MODEL", "qwen2.5:7b")


def models() -> list[str]:
    try:
        with urllib.request.urlopen(BASE_URL + "/api/tags", timeout=5) as response:
            payload = json.load(response)
        return [entry["name"] for entry in payload.get("models", []) if entry.get("name")]
    except (OSError, ValueError, KeyError, urllib.error.URLError):
        return []


def ask_json(model: str, system: str, user: str, *, tokens: int = 500) -> dict:
    available = models()
    if not available:
        raise RuntimeError("Ollama is unavailable. Start Ollama and download a local model before generating.")
    if model not in available:
        raise ValueError(f"Local model {model!r} is not installed. Choose one of: {', '.join(available)}")
    request = urllib.request.Request(
        BASE_URL + "/api/chat",
        data=json.dumps({"model": model, "stream": False, "format": "json", "keep_alive": "15m",
                         "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                         "options": {"temperature": 0.1, "num_predict": tokens, "num_ctx": 8192}}).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = json.load(response)
        content = payload.get("message", {}).get("content", "")
        result = json.loads(content)
        if not isinstance(result, dict):
            raise ValueError("The local model did not return a JSON object")
        return result
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"The local model did not respond: {exc}") from exc
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        raise RuntimeError(f"The local model returned an unusable plan: {exc}") from exc
