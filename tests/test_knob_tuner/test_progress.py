"""Unit tests for the progress callback helper."""

from src.knob_tuner.tools.progress import make_progress_callback


def test_emits_by_default_without_verbose(tmp_path, capsys):
    log_file = tmp_path / "p.log"
    emit = make_progress_callback(log_file=str(log_file), verbose=False)
    emit("hello")
    emit("world")

    out = capsys.readouterr().out
    assert "hello" in out
    assert "world" in out

    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert "hello" in lines[0]
    assert "world" in lines[1]


def test_verbose_prints_and_appends(tmp_path, capsys):
    log_file = tmp_path / "p.log"
    emit = make_progress_callback(log_file=str(log_file), verbose=True)
    emit("hello")
    emit("world")

    out = capsys.readouterr().out
    assert "hello" in out
    assert "world" in out

    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("[")
    assert "hello" in lines[0]
    assert "world" in lines[1]


def test_log_file_failure_warns_instead_of_silently_passing(tmp_path, capsys):
    # Point at an unwritable location so the append fails.
    bad_log = tmp_path / "missing-dir" / "p.log"
    emit = make_progress_callback(log_file=str(bad_log), verbose=False)
    emit("hello")

    out = capsys.readouterr()
    assert "hello" in out.out
    assert "warning" in out.err and "cannot write log file" in out.err
    assert not bad_log.exists()
