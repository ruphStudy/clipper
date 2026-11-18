"""Speech-to-text backends producing one normalized transcript schema.

Schema: {"source_id","language","duration","backend","model","created_at",
         "segments":[{"start","end","text","words":[{"w","s","e","p"}]}]}  (seconds, absolute, 2 decimals)
"""
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import httpx

from app.config import settings
from app.logger import get_logger

log = get_logger("clipflow.transcribe")

OPENAI_URL = "https://api.openai.com/v1/audio/transcriptions"
OPENAI_TIMEOUT = 300.0
OPENAI_RETRIES = 3
LANG_CODES = {"english": "en", "hindi": "hi", "spanish": "es", "french": "fr", "german": "de", "portuguese": "pt",
              "italian": "it", "japanese": "ja", "korean": "ko", "chinese": "zh", "russian": "ru", "arabic": "ar",
              "turkish": "tr", "dutch": "nl", "indonesian": "id", "urdu": "ur", "bengali": "bn", "tamil": "ta"}


class TranscribeError(Exception):
    """Transcription failed (message is short and safe to show)."""


def r2(x: Optional[float]) -> Optional[float]:
    return None if x is None else round(float(x), 2)


def _word(w: str, s: float, e: float, p: Optional[float], offset: float = 0.0) -> dict:
    return {"w": w.strip(), "s": r2(s + offset), "e": r2(e + offset), "p": r2(p) if p is not None else None}


def build_transcript(source_id: int, language: Optional[str], duration: Optional[float], backend: str, model: str,
                     segments: list[dict]) -> dict:
    """Assemble the transcript dict (segments already normalized/absolute)."""
    return {"source_id": source_id, "language": language, "duration": r2(duration), "backend": backend,
            "model": model, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "segments": segments}


def stats(transcript: dict) -> dict:
    """word_count and speech_ratio (speech seconds / duration, 0–1)."""
    segs = transcript.get("segments") or []
    words = sum(len(s.get("words") or []) for s in segs)
    speech = sum(max(0.0, (s["end"] or 0) - (s["start"] or 0)) for s in segs)
    duration = transcript.get("duration") or (segs[-1]["end"] if segs else 0) or 0
    ratio = min(1.0, speech / duration) if duration else 0.0
    return {"word_count": words, "speech_ratio": round(ratio, 3)}


class LocalBackend:
    """faster-whisper on CPU (int8); the model is loaded once per process."""

    name = "local"
    _models: dict = {}

    def __init__(self, model: Optional[str] = None, compute: Optional[str] = None,
                 model_factory: Optional[Callable] = None, decoder: Optional[Callable] = None) -> None:
        self.model_name = model or settings.WHISPER_MODEL
        self.compute = compute or settings.WHISPER_COMPUTE
        self.model_factory = model_factory
        # Decode with ffmpeg ourselves: faster-whisper's PyAV path breaks on newer PyAV releases.
        self.decoder = decoder
        self.audio_format = "flac"

    def _model(self):
        key = (self.model_name, self.compute)
        if key not in LocalBackend._models:
            factory = self.model_factory
            if factory is None:
                from faster_whisper import WhisperModel
                factory = WhisperModel
            log.info("loading whisper model %s (%s)", self.model_name, self.compute)
            LocalBackend._models[key] = factory(self.model_name, device="cpu", compute_type=self.compute)
        return LocalBackend._models[key]

    def transcribe(self, audio_path: Path, source_id: int, language: Optional[str]) -> dict:
        try:
            if self.decoder is None:
                from app.clipper.audio import decode_pcm
                self.decoder = decode_pcm
            segments, info = self._model().transcribe(
                self.decoder(audio_path), word_timestamps=True, vad_filter=True, beam_size=1, language=language,
                condition_on_previous_text=False)
            out = []
            for seg in segments:
                words = [_word(w.word, w.start, w.end, w.probability) for w in (seg.words or [])]
                out.append({"start": r2(seg.start), "end": r2(seg.end), "text": seg.text.strip(),
                            "words": [w for w in words if w["w"]]})
        except TranscribeError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise TranscribeError(f"{type(exc).__name__}: {exc}") from exc
        return build_transcript(source_id, getattr(info, "language", language), getattr(info, "duration", None),
                                self.name, self.model_name, out)


class OpenAIBackend:
    """OpenAI transcription API (whisper-1 for word timestamps), chunked to stay under 25 MB."""

    name = "openai"

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None,
                 client: Optional[httpx.Client] = None, sleep: Callable[[float], None] = time.sleep) -> None:
        self.api_key = settings.OPENAI_API_KEY if api_key is None else api_key
        self.model_name = model or settings.OPENAI_TRANSCRIBE_MODEL
        self.client = client or httpx.Client(timeout=OPENAI_TIMEOUT)
        self.sleep = sleep
        self.audio_format = "mp3"

    def _post(self, chunk: Path, language: Optional[str]) -> dict:
        data = {"model": self.model_name, "response_format": "verbose_json",
                "timestamp_granularities[]": ["word", "segment"]}
        if language:
            data["language"] = language
        last = ""
        for attempt in range(1, OPENAI_RETRIES + 1):
            try:
                with chunk.open("rb") as fh:
                    resp = self.client.post(OPENAI_URL, headers={"Authorization": f"Bearer {self.api_key}"},
                                            data=data, files={"file": (chunk.name, fh, "audio/mpeg")},
                                            timeout=OPENAI_TIMEOUT)
            except httpx.HTTPError as exc:
                last = f"network error: {type(exc).__name__}"
            else:
                if resp.status_code == 200:
                    return resp.json()
                last = f"HTTP {resp.status_code}"
                if resp.status_code != 429 and resp.status_code < 500:
                    try:
                        detail = (resp.json().get("error") or {}).get("message", "")
                    except ValueError:
                        detail = ""
                    raise TranscribeError(f"openai {last} {detail}".strip()[:200])
            if attempt < OPENAI_RETRIES:
                log.warning("openai transcription %s, retry %d", last, attempt)
                self.sleep(2 ** attempt)
        raise TranscribeError(f"openai {last} after {OPENAI_RETRIES} attempts")

    def transcribe_chunks(self, chunks: list[tuple[Path, float]], source_id: int, language: Optional[str],
                          duration: Optional[float]) -> dict:
        if not self.api_key:
            raise TranscribeError("OPENAI_API_KEY not set")
        segments: list[dict] = []
        detected = None
        for path, offset in chunks:
            data = self._post(path, language)
            detected = detected or data.get("language")
            words = [_word(w.get("word", ""), w["start"], w["end"], None, offset) for w in data.get("words") or []]
            words = [w for w in words if w["w"]]
            segs = data.get("segments") or [{"start": 0, "end": data.get("duration") or 0, "text": data.get("text", "")}]
            words.sort(key=lambda w: w["s"])
            j = 0
            for i, seg in enumerate(segs):
                start, end = r2(seg["start"] + offset), r2(seg["end"] + offset)
                last_seg = i == len(segs) - 1
                inside = []
                while j < len(words) and (last_seg or words[j]["s"] < end):
                    inside.append(words[j])  # words before the first segment start join the first segment
                    j += 1
                segments.append({"start": start, "end": end, "text": (seg.get("text") or "").strip(),
                                 "words": inside})
        lang = LANG_CODES.get((detected or "").lower(), detected or language)
        return build_transcript(source_id, lang, duration, self.name, self.model_name, segments)


def get_backend(name: Optional[str] = None):
    """Backend instance by name (default TRANSCRIBE_BACKEND)."""
    name = (name or settings.TRANSCRIBE_BACKEND or "local").lower()
    if name == "openai":
        return OpenAIBackend()
    if name == "local":
        return LocalBackend()
    raise TranscribeError(f"unknown TRANSCRIBE_BACKEND: {name}")
