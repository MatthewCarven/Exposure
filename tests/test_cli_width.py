"""Tables and listings fit the terminal: no line longer than the width."""
import pytest

from conftest import needs_314
from exposure import cli
from exposure.client import Client


@pytest.fixture
def narrow(monkeypatch):
    monkeypatch.setenv("COLUMNS", "81")      # term_width() = 80 when not at a tty
    return 80


def test_table_fits(narrow, capsys):
    rows = [("@1", "a_rather_long_variable_name", "policy", "rc", "x" * 300),
            ("", "short", "local", "", "<pysynthrack.ui.app.App>")]
    cli._table(rows, ["handle", "name", "kind", "perm", "value"], flex=4)
    lines = capsys.readouterr().out.splitlines()
    assert lines and all(len(line) <= narrow for line in lines)
    assert lines[1].endswith("…")


def test_table_fits_with_many_wide_columns(narrow, capsys):
    rows = [tuple("y" * 60 for _ in range(6))]
    cli._table(rows, [f"col{i}" for i in range(6)])
    assert all(len(line) <= narrow for line in capsys.readouterr().out.splitlines())


def test_kind_and_path_shortening():
    assert cli._short_kind("policy:<entry>:self._on_toggle_audio") == "policy"
    assert cli._short_kind("expose:config") == "exposed"
    assert cli._short_kind("local") == "local"
    assert cli._short_path(r"C:\a\b\c\pysynthrack\ui\app.py") == "…/pysynthrack/ui/app.py"


@needs_314
def test_real_listings_fit(narrow, make_target, tmp_path, capsys):
    (tmp_path / "exposure.toml").write_text(
        '[agent]\nscope = "both"\n[expose]\n'
        '"<entry>:player" = { read = true, write = ["hp", "speed"], call = ["heal"] }\n',
        encoding="utf-8")
    t = make_target(script="game_loop.py", cwd=str(tmp_path))
    with Client.attach(t.pid) as c:
        for cmd, res in (("ls", c.ls()), ("ls", c.ls("player")), ("threads", c.threads()),
                         ("stack", c.stack()), ("find", c.find("name:*")),
                         ("history", c.history()), ("jobs", c.jobs())):
            cli.print_result(cmd, res)
    lines = capsys.readouterr().out.splitlines()
    too_long = [line for line in lines if len(line) > narrow]
    assert lines and not too_long, too_long
