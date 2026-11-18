"""Minimal Gemini REST client returning structured JSON (no SDK)."""
import json
import time
from typing import Callable, Optional

import httpx
from sqlalchemy import Engine

from app.ai.usage import record_usage
from app.config import settings
from app.logger import get_logger

log = get_logger("clipflow.gemini")

URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
TIMEOUT = 120.0
BACKOFF = (2, 6, 15)
_NO_THINKING: set[str] = set()  # models that rejected thinkingConfig (cached for this process)


def _usage(data: dict) -> tuple[int, int]:
    meta = data.get("usageMetadata") or {}
    out = (meta.get("candidatesTokenCount") or 0) + (meta.get("thoughtsTokenCount") or 0)
    return int(meta.get("promptTokenCount") or 0), int(out)


def _text(data: dict) -> Optional[str]:
    try:
        parts = data["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, TypeError):
        return None
    return "".join(p.get("text", "") for p in parts) or None


def generate_json(prompt: str, schema: dict, purpose: str, max_output_tokens: int = 1200,
                  client: Optional[httpx.Client] = None, engine: Optional[Engine] = None,
                  sleep: Callable[[float], None] = time.sleep) -> Optional[dict]:
    """One structured-output call; returns parsed JSON or None. Every HTTP call is recorded in ai_usage."""
    key, model = settings.GEMINI_API_KEY, settings.GEMINI_MODEL
    if not key:
        log.info("gemini skipped: GEMINI_API_KEY not set")
        return None
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT)
    try:
        attempt = 0
        while True:
            config = {"temperature": 0.4, "responseMimeType": "application/json", "responseSchema": schema,
                      "maxOutputTokens": max_output_tokens}
            with_thinking = model not in _NO_THINKING
            if with_thinking:
                config["thinkingConfig"] = {"thinkingBudget": 0}
            body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": config}
            status, data = 0, {}
            try:
                resp = client.post(URL.format(model=model), headers={"x-goog-api-key": key}, json=body,
                                   timeout=TIMEOUT)
                status = resp.status_code
                try:
                    data = resp.json()
                except ValueError:
                    data = {}
            except httpx.HTTPError as exc:
                log.warning("gemini %s network error: %s", purpose, type(exc).__name__)
            tin, tout = _usage(data)
            if status == 200:
                text = _text(data)
                try:
                    parsed = json.loads(text) if text else None
                except ValueError:
                    parsed = None
                record_usage(purpose, model, tin, tout, parsed is not None, engine)
                if parsed is None:
                    log.warning("gemini %s: unparseable response (finish=%s)", purpose,
                                ((data.get("candidates") or [{}])[0]).get("finishReason"))
                return parsed
            record_usage(purpose, model, tin, tout, False, engine)
            message = str((data.get("error") or {}).get("message", ""))
            if status == 400 and with_thinking and "thinking" in message.lower():
                log.info("gemini model %s rejects thinkingConfig; retrying without it", model)
                _NO_THINKING.add(model)
                continue
            if (status == 0 or status == 429 or status >= 500) and attempt < len(BACKOFF):
                log.warning("gemini %s HTTP %s, retry in %ss", purpose, status or "error", BACKOFF[attempt])
                sleep(BACKOFF[attempt])
                attempt += 1
                continue
            log.error("gemini %s failed: HTTP %s %s", purpose, status, message[:200])
            return None
    finally:
        if own:
            client.close()
