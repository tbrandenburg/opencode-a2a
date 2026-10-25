from __future__ import annotations

import fcntl
import json
import os
import shutil
import socket
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux gateway process lifecycle")


def invoke(
    project: Path, environment: dict[str, str], target: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["make", target], cwd=project, env=environment, capture_output=True, text=True, timeout=30
    )


@pytest.fixture
def gateway_project(tmp_path: Path) -> Iterator[tuple[Path, dict[str, str]]]:
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    for name in ("Makefile", "pyproject.toml", "uv.lock", "scripts/gateway_process.py"):
        shutil.copyfile(ROOT / name, project / name)
    (project / ".venv").symlink_to(Path(sys.executable).parent.parent, target_is_directory=True)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    environment = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "UV_NO_SYNC": "1",
        "OPENCODE_API_VERSION": "v2",
        "OPENCODE_V2_EXECUTION": "disabled",
        "A2A_STATIC_AUTH_CREDENTIALS": json.dumps(
            [{"scheme": "bearer", "token": "make-test-token", "principal": "test"}]
        ),
        "A2A_HOST": "127.0.0.1",
        "A2A_PORT": str(port),
        "A2A_PUBLIC_URL": f"http://127.0.0.1:{port}",
        "A2A_TASK_STORE_BACKEND": "memory",
    }
    try:
        yield project, environment
    finally:
        result = invoke(project, environment, "stop")
        assert result.returncode == 0, result.stderr


def test_make_run_status_duplicate_and_stop_real_gateway(gateway_project) -> None:
    project, environment = gateway_project
    started = invoke(project, environment, "run")
    assert started.returncode == 0, started.stderr
    assert "Gateway listening" in started.stdout
    state = project / "run/gateway"
    original = json.loads((state / "process.json").read_text())

    with httpx.Client(base_url=environment["A2A_PUBLIC_URL"], timeout=5) as client:
        health = client.get("/health", headers={"Authorization": "Bearer make-test-token"})
        card = client.get("/.well-known/agent-card.json")
    assert health.status_code == 200
    assert card.json()["capabilities"]["streaming"] is False
    assert (state / "gateway.log").stat().st_mode & 0o777 == 0o600

    duplicate = invoke(project, environment, "run")
    status = invoke(project, environment, "status")
    assert duplicate.returncode == 0, duplicate.stderr
    assert json.loads((state / "process.json").read_text())["pid"] == original["pid"]
    assert status.returncode == 0
    assert f"PID {original['pid']}" in status.stdout

    stopped = invoke(project, environment, "stop")
    assert stopped.returncode == 0, stopped.stderr
    assert not (state / "process.json").exists()
    with socket.socket() as connection:
        connection.settimeout(2)
        assert connection.connect_ex(("127.0.0.1", int(environment["A2A_PORT"]))) != 0
    again = invoke(project, environment, "stop")
    assert again.returncode == 0
    assert "Gateway is stopped" in again.stdout


def test_make_run_rejects_missing_auth_without_starting(gateway_project) -> None:
    project, environment = gateway_project
    environment.pop("A2A_STATIC_AUTH_CREDENTIALS")
    result = invoke(project, environment, "run")
    assert result.returncode != 0
    assert "Invalid gateway configuration" in result.stderr
    assert not (project / "run/gateway/process.json").exists()


def test_make_run_refuses_occupied_port_without_stopping_listener(gateway_project) -> None:
    project, environment = gateway_project
    with socket.create_server(("127.0.0.1", 0)) as listener:
        environment["A2A_PORT"] = str(listener.getsockname()[1])
        result = invoke(project, environment, "run")
        assert result.returncode != 0
        assert not (project / "run/gateway/process.json").exists()
        with socket.create_connection(listener.getsockname(), timeout=2):
            connection, _ = listener.accept()
            connection.close()


def test_make_stop_refuses_unrelated_live_pid(gateway_project) -> None:
    project, environment = gateway_project
    state = project / "run/gateway"
    state.mkdir(parents=True)
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], env=environment
    )
    try:
        start = Path(f"/proc/{unrelated.pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        (state / "process.json").write_text(
            json.dumps({"pid": unrelated.pid, "start": f"{boot}:{start}", "descriptor": 9999})
        )
        with (state / "process.lock").open("a+") as lifetime:
            fcntl.flock(lifetime, fcntl.LOCK_EX)
            result = invoke(project, environment, "stop")
            assert result.returncode != 0
            assert unrelated.poll() is None
            assert (state / "process.json").exists()
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=5)


def test_make_clean_preserves_runtime_state_and_database(gateway_project) -> None:
    project, environment = gateway_project
    (project / "dist").mkdir()
    artifact = project / "dist/example.whl"
    artifact.touch()
    state = project / "run/gateway"
    state.mkdir(parents=True)
    (state / "gateway.log").write_text("runtime log")
    database = project / "opencode-a2a.db"
    database.write_text("persistent state")
    result = invoke(project, environment, "clean")
    assert result.returncode == 0, result.stderr
    assert not artifact.exists()
    assert database.read_text() == "persistent state"
    assert (state / "gateway.log").read_text() == "runtime log"
