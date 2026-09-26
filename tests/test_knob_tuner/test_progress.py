"""Unit tests for the verbose progress callback helper."""

from src.knob_tuner.tools.progress import make_progress_callback


def test_silent_when_not_verbose(tmp_path, capsys):
    log_file = tmp_path / "p.log"
    emit = make_progress_callback(log_file=str(log_file), verbose=False)
    emit("hello")
    emit("world")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert not log_file.exists()


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
