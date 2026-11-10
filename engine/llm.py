"""Thin, dependency-free LLM layer.

Providers:
  replay     - read committed responses from engine/cache/ (default; zero cost,
               reproducible; this is how results.json in the repo was built)
  anthropic  - Claude via the Messages API      (ANTHROPIC_API_KEY)
  openai     - Chat Completions, JSON mode       (OPENAI_API_KEY)
  gemini     - Gemini generateContent, JSON mode (GEMINI_API_KEY; free tier works)

Every live response is written to the cache keyed by (stage, prompt version,
question id, variant), so a re-run never pays twice for the same call and the
exact model output behind every decision is auditable in git.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from pathlib import Path

CACHE = Path(__file__).resolve().parent / "cache"

DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-5-5",
    "openai": "gpt-4.1-mini",
    "gemini": "gemini-2.5-flash",
}


class LLMError(RuntimeError):
    pass


def _cache_path(stage: str, version: str, key: str) -> Path:
    return CACHE / stage / version / f"{key}.json"


def _extract_json(text: str):
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1)
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
    if start < 0:
        raise LLMError("no JSON object in model output")
    return json.loads(text[start:])


def _post(url: str, payload: dict, headers: dict, timeout: int = 120) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"content-type": "application/json", **headers})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except Exception as e:  # rate limits / transient network
            if attempt == 2:
                raise LLMError(str(e)) from e
            time.sleep(2 ** attempt * 2)


class LLM:
    def __init__(self, provider: str = "replay", model: str | None = None, temperature: float = 0.0):
        self.provider = provider
        self.model = model or DEFAULT_MODELS.get(provider, "replay")
        self.temperature = temperature
        self.calls = 0
        self.cache_hits = 0
        self.meter = []  # one row per call: stage, input chars, output chars (cache hits included)

    def json_call(self, stage: str, version: str, key: str, system: str, user: str) -> dict:
        path = _cache_path(stage, version, key)
        if path.exists():
            self.cache_hits += 1
            response = json.loads(path.read_text(encoding="utf-8"))["response"]
            self.meter.append({"stage": stage, "system": len(system), "user": len(user), "output": len(json.dumps(response, ensure_ascii=False))})
            return response
        if self.provider == "replay":
            raise LLMError(f"replay cache miss: {path.relative_to(CACHE.parent)} (run with a live provider)")
        text = getattr(self, f"_{self.provider}")(system, user)
        self.calls += 1
        response = _extract_json(text)
        self.meter.append({"stage": stage, "system": len(system), "user": len(user), "output": len(text)})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"stage": stage, "version": version, "provider": self.provider, "model": self.model,
                                    "response": response}, indent=1, ensure_ascii=False), encoding="utf-8")
        return response

    # -- providers -----------------------------------------------------------
    def _anthropic(self, system: str, user: str) -> str:
        out = _post(
            "https://api.anthropic.com/v1/messages",
            {"model": self.model, "max_tokens": 4000, "temperature": self.temperature, "system": system,
             "messages": [{"role": "user", "content": user}]},
            {"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"},
        )
        return "".join(b.get("text", "") for b in out.get("content", []))

    def _openai(self, system: str, user: str) -> str:
        out = _post(
            "https://api.openai.com/v1/chat/completions",
            {"model": self.model, "temperature": self.temperature, "response_format": {"type": "json_object"},
             "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
            {"authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
        )
        return out["choices"][0]["message"]["content"]

    def _gemini(self, system: str, user: str) -> str:
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
               f"?key={os.environ['GEMINI_API_KEY']}")
        out = _post(url, {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": self.temperature, "responseMimeType": "application/json"},
        }, {})
        return out["candidates"][0]["content"]["parts"][0]["text"]
