"""One OpenAI-compatible client for every provider (Groq, OpenRouter, OpenAI, Ollama, any custom endpoint).

Swapping models = changing env vars / CLI flags. No provider-specific code paths.
  LLM_PROVIDER  groq | openrouter | openai | ollama | custom | none
  LLM_BASE_URL  overrides the preset (required for custom)
  LLM_API_KEY   or the provider-specific var (GROQ_API_KEY, OPENROUTER_API_KEY, OPENAI_API_KEY)
  LLM_MODEL     overrides the preset model
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field

import requests

PRESETS = {
    "groq": {"base_url": "https://api.groq.com/openai/v1", "model": "openai/gpt-oss-120b", "key_env": "GROQ_API_KEY"},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "model": "openai/gpt-oss-120b:free", "key_env": "OPENROUTER_API_KEY"},
    "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini", "key_env": "OPENAI_API_KEY"},
    "ollama": {"base_url": "http://localhost:11434/v1", "model": "llama3.1:8b", "key_env": ""},
    "custom": {"base_url": "", "model": "", "key_env": ""},
    "none": {"base_url": "", "model": "rules-only-baseline", "key_env": ""},
}


class LLMError(RuntimeError):
    pass


class DailyLimitError(LLMError):
    """Provider's per-day quota is used up: stop cleanly; the cache lets the run resume later."""


@dataclass
class LLMConfig:
    provider: str = "groq"
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    backup_keys: list = field(default_factory=list)   # failover ONLY for invalid/revoked keys (401/403)
    temperature: float = 0.0
    timeout: int = 120
    max_retries: int = 5
    usage: dict = field(default_factory=lambda: {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "retries": 0})

    @property
    def enabled(self) -> bool:
        return self.provider != "none"

    @classmethod
    def from_env(cls, provider: str | None = None, model: str | None = None,
                 base_url: str | None = None, api_key: str | None = None) -> "LLMConfig":
        provider = (provider or os.getenv("LLM_PROVIDER") or "groq").lower()
        if provider not in PRESETS:
            raise LLMError(f"Unknown provider '{provider}'. Choose from {list(PRESETS)}.")
        p = PRESETS[provider]
        key = api_key or os.getenv("LLM_API_KEY") or (os.getenv(p["key_env"]) if p["key_env"] else "") or ""
        keys = [k.strip() for k in key.split(",") if k.strip()]
        key = keys[0] if keys else ""
        cfg = cls(provider=provider, backup_keys=keys[1:],
                  base_url=(base_url or os.getenv("LLM_BASE_URL") or p["base_url"]).rstrip("/"),
                  model=model or os.getenv("LLM_MODEL") or p["model"],
                  api_key=key)
        if cfg.enabled and not cfg.base_url:
            raise LLMError("custom provider needs LLM_BASE_URL (or --base-url).")
        if cfg.enabled and not cfg.model:
            raise LLMError("No model set: pass --model or LLM_MODEL.")
        if cfg.enabled and provider not in ("ollama", "custom") and not cfg.api_key:
            raise LLMError(f"No API key: set {p['key_env'] or 'LLM_API_KEY'} (or use --provider none).")
        return cfg

    def describe(self) -> dict:
        return {"provider": self.provider, "base_url": self.base_url, "model": self.model, "temperature": self.temperature}


def extract_json(text: str) -> dict:
    """Parse JSON even if the model wrapped it in prose or ```json fences."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"[\{\[].*[\}\]]", text, re.S)
        if not m:
            raise
        return json.loads(m.group(0))


def _post(cfg: LLMConfig, payload: dict) -> requests.Response:
    headers = {"Content-Type": "application/json"}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"
    return requests.post(f"{cfg.base_url}/chat/completions", headers=headers, json=payload, timeout=cfg.timeout)


def chat_json(cfg: LLMConfig, system: str, user: str, max_tokens: int = 2500) -> tuple[dict, str]:
    """Return (parsed_json, raw_text). Handles 429/5xx backoff, endpoints without JSON mode, and one repair retry."""
    if not cfg.enabled:
        raise LLMError("LLM disabled (provider=none).")
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    use_json_mode = True
    raw = ""
    for repair in range(2):                     # attempt 0 = normal, 1 = ask model to fix its JSON
        attempt = 0
        while True:
            payload = {"model": cfg.model, "messages": messages, "temperature": cfg.temperature, "max_tokens": max_tokens}
            if cfg.provider == "groq" and "gpt-oss" in cfg.model:
                payload["reasoning_effort"] = "low"      # reasoning model: keep hidden thinking tokens (and cost) small
            if use_json_mode:
                payload["response_format"] = {"type": "json_object"}
            try:
                r = _post(cfg, payload)
            except requests.RequestException as ex:
                r, err = None, str(ex)
            if r is not None and r.status_code == 200:
                break
            if r is not None and r.status_code == 400 and use_json_mode and re.search(r"response_format|json_validate_failed|failed to generate json", r.text, re.I):
                use_json_mode = False           # endpoint doesn't support JSON mode -> plain prompt + extraction
                continue
            if r is not None and r.status_code in (401, 403) and cfg.backup_keys:
                cfg.api_key = cfg.backup_keys.pop(0)      # bad/revoked key -> fail over to the next one
                continue
            if r is not None and r.status_code == 429 and re.search(r"per day|\bTPD\b|\bRPD\b", r.text, re.I):
                raise DailyLimitError(f"Daily quota reached for {cfg.model}: {r.text[:200]}")
            retryable = r is None or r.status_code in (408, 409, 429, 500, 502, 503, 504)
            attempt += 1
            if not retryable or attempt > cfg.max_retries:
                detail = err if r is None else f"HTTP {r.status_code}: {r.text[:300]}"
                raise LLMError(detail)
            cfg.usage["retries"] += 1
            wait = 2 ** attempt
            if r is not None:
                try:
                    wait = max(wait, float(r.headers.get("retry-after", 0)))
                except ValueError:
                    pass
            time.sleep(min(wait, 90))
        data = r.json()
        cfg.usage["calls"] += 1
        u = data.get("usage") or {}
        cfg.usage["prompt_tokens"] += int(u.get("prompt_tokens") or 0)
        cfg.usage["completion_tokens"] += int(u.get("completion_tokens") or 0)
        raw = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        try:
            return extract_json(raw), raw
        except (json.JSONDecodeError, ValueError):
            messages = messages + [{"role": "assistant", "content": raw},
                                   {"role": "user", "content": "That was not valid JSON. Reply with ONLY the corrected JSON object."}]
    raise LLMError(f"Model did not return valid JSON. Raw start: {raw[:200]!r}")
