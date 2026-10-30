"""ServiceSettings: subclasses inherit the shared config -- unknown keys ignored, fields read by alias."""

from __future__ import annotations

from pydantic import Field

from edge_platform.service.settings import ServiceSettings


class _S(ServiceSettings):
    port: int = Field(default=1, alias="SVC_PORT")


def test_inherits_config(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SVC_PORT", "8091")
    monkeypatch.setenv("SOME_OTHER_SERVICE_KEY", "x")  # the shared .env: other services' keys
    s = _S()
    assert s.port == 8091
    assert _S.model_config["extra"] == "ignore" and _S.model_config["env_file"] == ".env"


def test_reads_dotenv(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SVC_PORT", raising=False)
    (tmp_path / ".env").write_text("SVC_PORT=7\nUNRELATED=1\n", encoding="utf-8")
    assert _S().port == 7
