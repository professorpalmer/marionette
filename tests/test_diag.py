from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from types import SimpleNamespace

import pytest

from harness import diag
from harness.correlation import correlation_scope


@pytest.fixture(autouse=True)
def _isolated_logger(tmp_path, monkeypatch):
    logger = logging.Logger("pmharness.diag")
    private_logging = SimpleNamespace(**vars(logging))
    private_logging.getLogger = lambda _name: logger
    monkeypatch.setattr(diag, "logging", private_logging)
    monkeypatch.setattr(diag, "_logger", None)
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    yield
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)


def _flush(logger: logging.Logger) -> None:
    for handler in logger.handlers:
        handler.flush()


def test_diagnostics_log_rolls_over_and_retains_bounded_backups(tmp_path, monkeypatch):
    monkeypatch.setattr(diag, "_MAX_BYTES", 180, raising=False)
    monkeypatch.setattr(diag, "_BACKUP_COUNT", 3, raising=False)

    logger = diag._get_logger()
    for index in range(20):
        logger.info("entry-%02d %s", index, "x" * 80)
    _flush(logger)

    assert isinstance(logger.handlers[0], RotatingFileHandler)
    assert (tmp_path / "diagnostics.log").is_file()
    assert sorted(path.name for path in tmp_path.glob("diagnostics.log.*")) == [
        "diagnostics.log.1",
        "diagnostics.log.2",
        "diagnostics.log.3",
    ]


def test_oversized_existing_log_rolls_on_first_emit(tmp_path, monkeypatch):
    monkeypatch.setattr(diag, "_MAX_BYTES", 128, raising=False)
    monkeypatch.setattr(diag, "_BACKUP_COUNT", 3, raising=False)
    path = tmp_path / "diagnostics.log"
    path.write_text("old-log\n" * 40, encoding="utf-8")

    diag.note("oversized", msg="new-entry")
    _flush(diag._get_logger())

    assert "old-log" in (tmp_path / "diagnostics.log.1").read_text(encoding="utf-8")
    assert "oversized: new-entry" in path.read_text(encoding="utf-8")


def test_formatter_and_correlation_are_preserved(tmp_path):
    with correlation_scope("diag-correlation"):
        diag.note("unit.test", RuntimeError("boom"), msg="failed")
    _flush(diag._get_logger())

    line = (tmp_path / "diagnostics.log").read_text(encoding="utf-8")
    assert " WARNING " in line
    assert "[diag-correlation] unit.test: failed RuntimeError('boom')" in line


def test_file_open_failure_falls_back_to_null_handler(monkeypatch):

    def fail_open(*_args, **_kwargs):
        raise OSError("read only")

    monkeypatch.setattr(diag, "RotatingFileHandler", fail_open, raising=False)
    logger = diag._get_logger()

    assert len(logger.handlers) == 1
    assert isinstance(logger.handlers[0], logging.NullHandler)
    diag.note("still.best.effort", msg="does not raise")
