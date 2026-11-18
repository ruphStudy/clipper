"""Transcription tests: real ffmpeg audio, mocked whisper model and OpenAI API, no network."""
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app import config
from app.clipper import audio, transcribe_service
from app.clipper.transcribe import LocalBackend, OpenAIBackend, TranscribeError, stats
from app.clipper.transcribe_service import transcribe_pending, transcribe_source, words_in_range
from app.db import get_engine, get_session, init_db
from app.db.enums import CampaignStatus, SourceKind, SourceStatus
from app.db.models import Campaign, Source
from app.downloader.media import probe
from app.scheduler.jobs import build_scheduler

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def make_video(path: Path, seconds: int = 3) -> Path:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=160x120:rate=10",
                    "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-shortest",
                    "-c:v", "mpeg4", "-c:a", "aac", str(path)], check=True, capture_output=True)
    return path


@pytest.fixture(scope="module")
def video(tmp_path_factory) -> Path:
    return make_video(tmp_path_factory.mktemp("v") / "clip.mp4")


def test_extract_audio_mono_16k(video, tmp_path):
    for fmt in ("flac", "mp3"):
        out = audio.extract_audio(video, tmp_path / f"a.{fmt}", fmt)
        info = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                               "stream=channels,sample_rate", "-of", "json", str(out)],
                              capture_output=True, text=True, check=True)
        stream = json.loads(info.stdout)["streams"][0]
        assert stream["channels"] == 1 and stream["sample_rate"] == "16000"
        assert probe(out)["duration_sec"] == pytest.approx(3, abs=0.2)


def test_decode_pcm(video):
    pcm = audio.decode_pcm(video)
    assert pcm.dtype.name == "float32" and abs(len(pcm) / 16000 - 3) < 0.2


def test_split_audio_offsets(tmp_path):
    src = tmp_path / "long.mp3"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=300:duration=25",
                    "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "32k", str(src)], check=True)
    chunks = audio.split_audio(src, chunk_sec=10)
    assert [off for _, off in chunks] == [0.0, 10.0, 20.0]
    assert all(p.exists() for p, _ in chunks)
    assert probe(chunks[-1][0])["duration_sec"] == pytest.approx(5, abs=0.3)


def fake_model_factory(segments, language="en", duration=10.0, calls=None):
    def factory(name, device, compute_type):
        assert device == "cpu"

        class Model:
            def transcribe(self, path, **kw):
                if calls is not None:
                    calls.append(kw)
                return iter(segments), SimpleNamespace(language=language, duration=duration)

        return Model()

    return factory


def seg(start, end, text, words):
    return SimpleNamespace(start=start, end=end, text=text,
                           words=[SimpleNamespace(word=w, start=s, end=e, probability=p) for w, s, e, p in words])


@pytest.fixture(autouse=True)
def fresh_model_cache():
    LocalBackend._models.clear()
    yield
    LocalBackend._models.clear()


def test_local_backend_schema_and_stats(tmp_path):
    calls: list = []
    segments = [seg(0.123, 2.456, " Hello world", [(" Hello", 0.123, 0.5, 0.987654), (" world", 0.6, 2.456, 0.9)]),
                seg(5.0, 7.0, " Quick fox", [(" Quick", 5.0, 5.5, 0.8), (" fox", 5.6, 7.0, 0.7)])]
    backend = LocalBackend(model="tiny", compute="int8", model_factory=fake_model_factory(segments, calls=calls),
                           decoder=str)
    t = backend.transcribe(tmp_path / "x.flac", 42, "en")
    assert set(t) == {"source_id", "language", "duration", "backend", "model", "created_at", "segments"}
    assert (t["source_id"], t["language"], t["duration"], t["backend"], t["model"]) == (42, "en", 10.0, "local", "tiny")
    first = t["segments"][0]
    assert (first["start"], first["end"], first["text"]) == (0.12, 2.46, "Hello world")
    assert first["words"][0] == {"w": "Hello", "s": 0.12, "e": 0.5, "p": 0.99}
    assert calls[0] == {"word_timestamps": True, "vad_filter": True, "beam_size": 1, "language": "en",
                        "condition_on_previous_text": False}
    st = stats(t)
    assert st["word_count"] == 4 and st["speech_ratio"] == pytest.approx((2.46 - 0.12 + 2.0) / 10, abs=0.001)


def test_openai_backend_two_chunks_and_429(tmp_path):
    chunk_a, chunk_b = tmp_path / "a.mp3", tmp_path / "b.mp3"
    chunk_a.write_bytes(b"a")
    chunk_b.write_bytes(b"b")
    replies = [
        httpx.Response(429, json={"error": {"message": "rate limit"}}),
        httpx.Response(200, json={"language": "english", "duration": 10, "text": "hi there",
                                  "segments": [{"start": 0.5, "end": 2.0, "text": " hi there"}],
                                  "words": [{"word": "hi", "start": 0.5, "end": 0.9},
                                            {"word": "there", "start": 1.0, "end": 2.0}]}),
        httpx.Response(200, json={"language": "english", "duration": 5, "text": "bye",
                                  "segments": [{"start": 1.0, "end": 1.5, "text": " bye"}],
                                  "words": [{"word": "bye", "start": 1.0, "end": 1.5}]}),
    ]
    seen = []

    def handler(request):
        seen.append(request)
        return replies.pop(0)

    sleeps: list = []
    backend = OpenAIBackend(api_key="sk-test-secret", model="whisper-1",
                            client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=sleeps.append)
    t = backend.transcribe_chunks([(chunk_a, 0.0), (chunk_b, 600.0)], 7, None, 605.0)
    assert len(seen) == 3 and sleeps == [2]
    assert seen[0].headers["authorization"] == "Bearer sk-test-secret"
    body = seen[0].content.decode(errors="ignore")
    assert "verbose_json" in body and body.count("timestamp_granularities[]") == 2
    words = [w for s in t["segments"] for w in s["words"]]
    assert [(w["w"], w["s"], w["e"], w["p"]) for w in words] == [("hi", 0.5, 0.9, None), ("there", 1.0, 2.0, None),
                                                                ("bye", 601.0, 601.5, None)]
    assert t["segments"][1]["start"] == 601.0 and t["language"] == "en" and t["backend"] == "openai"


def test_openai_requires_key(tmp_path):
    with pytest.raises(TranscribeError, match="OPENAI_API_KEY"):
        OpenAIBackend(api_key="").transcribe_chunks([(tmp_path / "a.mp3", 0.0)], 1, None, 1.0)


def test_words_in_range():
    t = {"segments": [{"start": 0, "end": 3, "words": [{"w": "a", "s": 0.0, "e": 0.5}, {"w": "b", "s": 1.0, "e": 1.5},
                                                     {"w": "c", "s": 2.8, "e": 3.2}]},
                      {"start": 4, "end": 6, "words": [{"w": "d", "s": 4.0, "e": 4.5}]}]}
    assert [w["w"] for w in words_in_range(t, 0.9, 4.6)] == ["b", "c", "d"]
    assert [w["w"] for w in words_in_range(t, 0.9, 3.0)] == ["b"]  # "c" ends at 3.2: partly outside
    assert words_in_range(t, 10, 20) == []


# --- service -------------------------------------------------------------------

class FakeBackend:
    name = "local"
    audio_format = "flac"

    def __init__(self, fail=False):
        self.fail = fail
        self.audio_seen: list[Path] = []

    def transcribe(self, audio_path, source_id, language):
        self.audio_seen.append(Path(audio_path))
        assert Path(audio_path).exists()
        if self.fail:
            raise TranscribeError("model crashed")
        return {"source_id": source_id, "language": language or "en", "duration": 3.0, "backend": "local",
                "model": "fake", "created_at": "x",
                "segments": [{"start": 0.0, "end": 1.5, "text": "quick brown fox",
                              "words": [{"w": "quick", "s": 0.0, "e": 0.4, "p": 0.9},
                                        {"w": "brown", "s": 0.5, "e": 0.9, "p": 0.9},
                                        {"w": "fox", "s": 1.0, "e": 1.5, "p": 0.9}]}]}


@pytest.fixture
def engine(tmp_path, monkeypatch, video):
    monkeypatch.setattr(config.settings, "TRANSCRIPTS_DIR", str(tmp_path / "transcripts"))
    monkeypatch.setattr(transcribe_service, "notify_error", lambda *a, **k: False)
    eng = init_db(get_engine(str(tmp_path / "t.db")))
    with get_session(eng) as s:
        s.add_all([Campaign(id=1, external_id="j", name="J", url="u", status=CampaignStatus.joined),
                   Campaign(id=2, external_id="d", name="D", url="u", status=CampaignStatus.discovered)])
    yield eng
    eng.dispose()


def add_source(engine, video, campaign_id=1, **kw) -> int:
    values = dict(campaign_id=campaign_id, url=f"https://a.test/{kw.get('url_id', id(kw))}.mp4",
                  kind=SourceKind.direct, status=SourceStatus.downloaded, local_path=str(video))
    kw.pop("url_id", None)
    values.update(kw)
    with get_session(engine) as s:
        src = Source(**values)
        s.add(src)
        s.flush()
        return src.id


def get(engine, sid) -> Source:
    with get_session(engine) as s:
        return s.get(Source, sid)


def test_transcribe_source_success(engine, video, tmp_path):
    sid = add_source(engine, video)
    backend = FakeBackend()
    assert transcribe_source(sid, engine, backend) is True
    src = get(engine, sid)
    assert src.status == SourceStatus.downloaded and src.error is None and src.word_count == 3
    assert src.language == "en" and src.speech_ratio == 0.5 and src.transcribed_at is not None
    data = json.loads(Path(src.transcript_path).read_text())
    assert data["segments"][0]["words"][2]["w"] == "fox"
    assert "\n" not in Path(src.transcript_path).read_text()
    assert not backend.audio_seen[0].exists()
    assert not any(p.name.startswith(".tmp_") for p in (tmp_path / "transcripts").iterdir())


def test_claim_skips_ineligible(engine, video, tmp_path):
    backend = FakeBackend()
    pending = add_source(engine, video, status=SourceStatus.pending, url_id="p")
    done = add_source(engine, video, transcript_path=str(tmp_path / "x.json"), url_id="d")
    tired = add_source(engine, video, transcribe_attempts=2, url_id="t")
    missing = add_source(engine, video, local_path=str(tmp_path / "gone.mp4"), url_id="m")
    for sid in (pending, done, tired, missing):
        assert transcribe_source(sid, engine, backend) is False
    assert backend.audio_seen == []
    assert get(engine, pending).status == SourceStatus.pending


def test_failure_increments_attempts_and_deletes_audio(engine, video, monkeypatch):
    alerts: list = []
    monkeypatch.setattr(transcribe_service, "notify_error", lambda where, exc: alerts.append(where))
    sid = add_source(engine, video)
    backend = FakeBackend(fail=True)
    assert transcribe_source(sid, engine, backend) is False
    src = get(engine, sid)
    assert src.transcribe_attempts == 1 and src.status == SourceStatus.downloaded
    assert src.error == "transcribe: model crashed" and src.transcript_path is None
    assert not backend.audio_seen[0].exists() and alerts == []
    assert transcribe_source(sid, engine, backend) is False
    assert get(engine, sid).transcribe_attempts == 2 and len(alerts) == 1
    assert transcribe_source(sid, engine, backend) is False and len(backend.audio_seen) == 2


def test_transcribe_pending_joined_only(engine, video):
    add_source(engine, video, campaign_id=2, url_id="other")
    joined = add_source(engine, video, campaign_id=1, url_id="mine")
    assert transcribe_pending(limit=5, engine=engine, backend=FakeBackend()) == {"done": 1, "failed": 0}
    assert get(engine, joined).transcript_path is not None


def test_reset_stuck_processing(engine, video):
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import update

    from app.downloader.service import reset_stuck

    sid = add_source(engine, video, status=SourceStatus.processing)
    with get_session(engine) as s:
        s.execute(update(Source).where(Source.id == sid)
                  .values(updated_at=datetime.now(timezone.utc) - timedelta(hours=4)))
    assert reset_stuck(engine=engine) == 1
    assert get(engine, sid).status == SourceStatus.downloaded


def test_scheduler_has_six_jobs():
    jobs = {j.id: j for j in build_scheduler().get_jobs()}
    assert len(jobs) >= 6 and "transcribe" in jobs
    assert jobs["transcribe"].trigger.interval.total_seconds() == 15 * 60
