"""Copying a shipped Windmill template into a consumer tree, and --check catching drift both ways."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from edge_platform import windmill
from edge_platform.windmill.__main__ import main


def test_post_admin_run_is_shipped() -> None:
    assert "post_admin_run" in windmill.names()


def test_copy_writes_three_files_with_the_lock_path_rewritten(tmp_path: Path) -> None:
    written = windmill.copy("post_admin_run", tmp_path, "u/admin/post_admin_run")
    assert sorted(p.name for p in written) == [
        "post_admin_run.py",
        "post_admin_run.script.lock",
        "post_admin_run.script.yaml",
    ]
    meta = yaml.safe_load((tmp_path / "u/admin/post_admin_run.script.yaml").read_text(encoding="utf-8"))
    assert meta["lock"] == "!inline u/admin/post_admin_run.script.lock"
    assert {"service_url", "kind"} <= set(meta["schema"]["required"])
    # required only when send_key is true (v0.10.1), which a static schema cannot say: main() enforces it
    assert "key_variable" not in meta["schema"]["required"]
    for prop in ("service_url", "key_variable"):
        assert "default" not in meta["schema"]["properties"][prop]  # REQUIRED: no default to fall back on
    assert b"\r\n" not in (tmp_path / "u/admin/post_admin_run.py").read_bytes()


def test_the_schema_names_every_main_parameter(tmp_path: Path) -> None:
    import ast

    src = windmill.render("post_admin_run", "u/a/b")[".py"]
    fn = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "main")
    meta = yaml.safe_load(windmill.render("post_admin_run", "u/a/b")[".script.yaml"])
    assert [a.arg for a in fn.args.args] == list(meta["schema"]["properties"])


def test_check_finds_drift_and_passes_a_fresh_copy(tmp_path: Path, capsys) -> None:
    args = ["copy", "post_admin_run", str(tmp_path), "f/ops/post_admin_run"]
    assert main([*args, "--check"]) == 1  # absent = drift
    assert not (tmp_path / "f").exists()  # --check wrote nothing
    assert main(args) == 0
    assert main([*args, "--check"]) == 0
    p = tmp_path / "f/ops/post_admin_run.py"
    p.write_text(p.read_text(encoding="utf-8") + "# hand edit\n", encoding="utf-8")
    assert main([*args, "--check"]) == 1
    assert "DRIFT" in capsys.readouterr().out


@pytest.mark.parametrize(("name", "path"), [("nope", "u/a/b"), ("post_admin_run", ""), ("post_admin_run", "u/a/b.py")])
def test_bad_input_is_exit_2(tmp_path: Path, name: str, path: str) -> None:
    assert main(["copy", name, str(tmp_path), path]) == 2


@pytest.mark.parametrize("pin", ["httpx==", "wmill=="])
def test_check_catches_a_lock_that_lost_a_pin(tmp_path: Path, pin: str, capsys) -> None:
    """The lock is one of the three compared files: a consumer copy whose lock lost its httpx / wmill
    pin (Windmill would resolve the latest at run time) is DRIFT, not a pass."""
    args = ["copy", "post_admin_run", str(tmp_path), "u/admin/post_admin_run"]
    assert main(args) == 0
    lock = tmp_path / "u/admin/post_admin_run.script.lock"
    text = lock.read_text(encoding="utf-8")
    assert pin in text
    lock.write_text("".join(ln for ln in text.splitlines(keepends=True) if not ln.startswith(pin)), encoding="utf-8")
    assert main([*args, "--check"]) == 1
    assert "post_admin_run.script.lock" in capsys.readouterr().out
