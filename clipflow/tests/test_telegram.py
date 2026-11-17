"""Telegram notifier tests (httpx.MockTransport, no network)."""
import json
import logging

import httpx

from app.notify import alerts, telegram
from app.notify.telegram import TelegramNotifier

TOKEN = "123456:SECRET-TOKEN-abcdef"
CHAT = "999"
OK = {"ok": True, "result": {"message_id": 1}}


def make(handler, token=TOKEN, chat_id=CHAT):
    """Notifier wired to a mock transport; returns (notifier, request log)."""
    calls: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    client = httpx.Client(transport=httpx.MockTransport(wrapped))
    return TelegramNotifier(token=token, chat_id=chat_id, client=client), calls


def ok_handler(_request):
    return httpx.Response(200, json=OK)


def test_disabled_makes_no_calls():
    n, calls = make(ok_handler, token="", chat_id="")
    assert n.enabled is False
    assert n.send_message("hi") is False
    assert calls == []


def test_send_message_payload():
    n, calls = make(ok_handler)
    assert n.send_message("hello") is True
    assert len(calls) == 1
    req = calls[0]
    assert str(req.url) == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    body = json.loads(req.content)
    assert body["chat_id"] == CHAT
    assert body["text"] == "hello"
    assert body["parse_mode"] == "HTML"
    assert body["disable_web_page_preview"] is True


def test_long_message_split_in_order():
    n, calls = make(ok_handler)
    lines = [f"{i:04d}" + "x" * 95 for i in range(45)]
    text = "\n".join(lines)
    assert len(text) > 4400
    assert n.send_message(text) is True
    assert len(calls) == 2
    sent = [json.loads(c.content)["text"] for c in calls]
    assert all(len(s) <= 4000 for s in sent)
    assert sent[0].startswith("0000") and sent[1].endswith(lines[-1])
    assert "\n".join(sent) == text


def test_429_retry_then_success(monkeypatch):
    sleeps = []
    monkeypatch.setattr(telegram.time, "sleep", sleeps.append)
    responses = iter([
        httpx.Response(429, json={"ok": False, "error_code": 429, "parameters": {"retry_after": 1}}),
        httpx.Response(200, json=OK),
    ])
    n, calls = make(lambda _r: next(responses))
    assert n.send_message("hi") is True
    assert len(calls) == 2
    assert sleeps == [1.0]


def test_500_returns_false():
    n, _ = make(lambda _r: httpx.Response(500, json={"ok": False, "description": "boom"}))
    assert n.send_message("hi") is False


def test_token_not_logged(caplog):
    def fail(request):
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    n, _ = make(fail)
    with caplog.at_level(logging.DEBUG):
        assert n.send_message("hi") is False
        n2, _ = make(lambda r: httpx.Response(500, text=f"bad {r.url}"))
        assert n2.send_message("hi") is False
    assert caplog.records
    assert TOKEN not in caplog.text


def test_notify_escapes_html(monkeypatch):
    n, calls = make(ok_handler)
    monkeypatch.setattr(alerts, "_notifier", n)
    assert alerts.notify("Title", "<script>alert(1)</script>", level="warning") is True
    text = json.loads(calls[0].content)["text"]
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    assert text.startswith("⚠️ <b>Title</b>\n")
