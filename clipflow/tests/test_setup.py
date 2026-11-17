"""Setup tests for config and logging."""
import logging
from pathlib import Path

from app import config, logger
from app.config import Settings, masked


def _use_tmp_dirs(monkeypatch, tmp_path):
    """Point all runtime dirs at tmp_path."""
    dirs = {
        "DATA_DIR": tmp_path / "data",
        "DOWNLOAD_DIR": tmp_path / "data" / "downloads",
        "CLIPS_DIR": tmp_path / "data" / "clips",
        "LOG_DIR": tmp_path / "data" / "logs",
    }
    for key, path in dirs.items():
        monkeypatch.setattr(config.settings, key, str(path))
    return dirs


def test_settings_defaults(monkeypatch):
    for name in Settings.model_fields:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    s = Settings(_env_file=None)
    assert s.APP_ENV == "dev"
    assert s.LOG_LEVEL == "INFO"
    assert s.TIMEZONE == "Asia/Kolkata"
    assert s.DB_PATH == "data/clipflow.db"
    assert s.DASHBOARD_USER == "admin"
    assert s.GEMINI_API_KEY == ""
    assert s.MAX_DISK_GB == 20


def test_ensure_dirs(monkeypatch, tmp_path):
    dirs = _use_tmp_dirs(monkeypatch, tmp_path)
    config.ensure_dirs()
    assert all(Path(p).is_dir() for p in dirs.values())


def test_masked():
    assert masked("abcdef1234") == "****1234"
    assert masked("") == ""


def test_get_logger_no_duplicate_handlers(monkeypatch, tmp_path):
    _use_tmp_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(logger, "_configured", False)
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        logger.get_logger("a")
        count = len(root.handlers)
        logger.get_logger("b")
        assert len(root.handlers) == count == len(before) + 2
        assert (tmp_path / "data" / "logs" / "clipflow.log").exists()
    finally:
        for handler in [h for h in root.handlers if h not in before]:
            root.removeHandler(handler)
            handler.close()
