import socket
import threading
import time

import pytest
import uvicorn
from app.cli import main
from app.main import app
from app.persistence.session import reset_engine


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_cli_submits_and_prints_the_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from pathlib import Path

    database = Path(str(tmp_path)) / "cli.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database}")
    monkeypatch.setenv("API_KEY", "dev-key")
    reset_engine()
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            raise AssertionError("API did not start")
        code = main(
            [
                "diagnose",
                "--scenario",
                "cpu-saturation-checkout",
                "--api",
                f"http://127.0.0.1:{port}",
                "--api-key",
                "dev-key",
            ]
        )
    finally:
        server.should_exit = True
        thread.join(timeout=5)
    captured = capsys.readouterr()
    assert code == 0
    assert "cpu_saturation" in captured.out
    assert "RUN_STARTED" in captured.out
