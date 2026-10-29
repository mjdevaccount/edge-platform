"""edge_platform.env: the python twin against compose's recorded answers, and check-copy both ways.

The full oracle (both twins, live docker compose, refusals) is tests/env/lib-env.test.sh, a CI
step; this file is what pytest can prove on any OS without bash, jq or docker.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from edge_platform.env import EnvBlind, cli, lib_env, load, parse

FX = Path(__file__).parent / "fixtures"
ENV = {"FX_OS_SET": "from-os", "FX_INHERIT": "inherited"}  # the process env the fixture promises


def _fixture_text() -> str:
    # the test script puts back the trailing spaces whitespace hygiene strips from the committed file
    lines = (FX / "fixture.dotenv").read_text(encoding="utf-8").split("\n")
    return "\n".join(ln + "   " if ln == "FX_TRAILING_WS=val" else ln for ln in lines)


@pytest.mark.parametrize("variant", ["lf", "crlf"])
def test_python_twin_gives_compose_answer(variant: str) -> None:
    want = json.loads((FX / "expected.json").read_text(encoding="utf-8"))[variant]
    text = _fixture_text()
    if variant == "crlf":
        text = text.replace("\n", "\r\n")
    got = parse(text, ENV)
    fx_keys = {k: v for k, v in got.items() if k.startswith("FX_")}
    assert fx_keys == want


def test_refuses_without_the_value() -> None:
    with pytest.raises(EnvBlind) as e:
        parse('FX_A="sekrit-123', {})
    assert "sekrit" not in str(e.value)


def test_package_reexports_the_module() -> None:
    assert load is lib_env.load and parse is lib_env.parse


def test_path_names_the_packaged_twins(capsys: pytest.CaptureFixture[str]) -> None:
    for kind, name in (("sh", "lib-env.sh"), ("py", "lib_env.py")):
        assert cli.main(["path", kind]) == 0
        p = Path(capsys.readouterr().out.strip())
        assert p.name == name and p.is_file()


def test_get_and_digest_pass_through(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    f = tmp_path / "a.env"
    f.write_text("K=v\n", encoding="utf-8")
    assert cli.main(["get", "K", str(f)]) == 0
    assert capsys.readouterr().out == "v"
    assert cli.main(["get", "NOPE", str(f)]) == 1
    assert cli.main(["digest", str(f), "K", "NOPE"]) == 0
    out = capsys.readouterr().out.split("\n")
    assert out[0].startswith("K ") and "v" not in out[0][2:] and out[1] == "NOPE absent"


def _copies(tmp_path: Path) -> tuple[Path, Path]:
    sh, py = tmp_path / "lib-env.sh", tmp_path / "lib_env.py"
    sh.write_bytes(cli.TWINS["lib-env.sh"].read_bytes())
    py.write_bytes(cli.TWINS["lib_env.py"].read_bytes())
    return sh, py


def test_check_copy_passes_identical_copies(tmp_path: Path) -> None:
    sh, py = _copies(tmp_path)
    assert cli.main(["check-copy", str(sh), str(py)]) == 0


def test_check_copy_fires_on_one_changed_byte(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sh, py = _copies(tmp_path)
    py.write_bytes(py.read_bytes().replace(b"LAST", b"FIRST", 1))
    assert cli.main(["check-copy", str(sh), str(py)]) == 1
    assert "lib_env.py" in capsys.readouterr().out


def test_check_copy_fires_on_crlf(tmp_path: Path) -> None:
    sh, _ = _copies(tmp_path)
    sh.write_bytes(sh.read_bytes().replace(b"\n", b"\r\n"))
    assert cli.main(["check-copy", str(sh)]) == 1


@pytest.mark.parametrize("name", ["missing/lib-env.sh", "notes.txt"])
def test_check_copy_is_blind_not_clean(tmp_path: Path, name: str) -> None:
    p = tmp_path / name
    if name == "notes.txt":
        p.write_text("x", encoding="utf-8")
    assert cli.main(["check-copy", str(p)]) == 2


def test_bad_usage_is_blind() -> None:
    assert cli.main([]) == 2
    assert cli.main(["check-copy"]) == 2
    assert cli.main(["path", "rb"]) == 2
