"""Linux-only local gateway lifecycle; never manages the upstream OpenCode process."""

from __future__ import annotations

import argparse
import ctypes
import fcntl
import ipaddress
import json
import os
import select
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import TextIO

from pydantic import ValidationError

from opencode_a2a.config import Settings

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ctypes.CDLL(None, use_errno=True)


def checked(result: int) -> int:
    if result < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return result


def open_process(pid: int) -> int:
    # Some Python builds omit pidfd wrappers even when libc/kernel support them.
    command = NATIVE.pidfd_open
    command.argtypes = (ctypes.c_int, ctypes.c_uint)
    command.restype = ctypes.c_int
    return checked(command(pid, 0))


def terminate_process(handle: int) -> None:
    command = NATIVE.pidfd_send_signal
    command.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint)
    command.restype = ctypes.c_int
    checked(command(handle, signal.SIGTERM, None, 0))


def fingerprint(pid: int) -> str:
    start = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    return f"{boot}:{start}"


def acquire(lock: TextIO) -> bool:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def identify(record: Path, lifetime: TextIO) -> tuple[int, int]:
    data = json.loads(record.read_text())
    if not isinstance(data, dict):
        raise ValueError("Invalid gateway process record; refusing to signal")
    pid, descriptor, start = data.get("pid"), data.get("descriptor"), data.get("start")
    if not isinstance(pid, int) or pid <= 1 or not isinstance(descriptor, int):
        raise ValueError("Invalid gateway process identity; refusing to signal")
    handle = open_process(pid)
    try:
        owned = Path(f"/proc/{pid}/fd/{descriptor}").stat()
        expected = os.fstat(lifetime.fileno())
        if start != fingerprint(pid) or (owned.st_dev, owned.st_ino) != (
            expected.st_dev,
            expected.st_ino,
        ):
            raise ValueError("Gateway process identity changed; refusing to signal")
    except BaseException:
        os.close(handle)
        raise
    return pid, handle


def start_gateway(state: Path, record: Path, lifetime: TextIO) -> None:
    settings = Settings()
    host, port = settings.a2a_host, settings.a2a_port
    resolved = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0]
    local = ipaddress.ip_address(resolved[4][0])
    address = ("::1" if local.version == 6 else "127.0.0.1") if local.is_unspecified else str(local)
    family = resolved[0]
    with socket.create_server((host, port), family=family):
        pass
    log = state / "gateway.log"
    with log.open("ab") as output:
        log.chmod(0o600)
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from opencode_a2a.cli import main; raise SystemExit(main(['serve']))",
            ],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            pass_fds=(lifetime.fileno(),),
        )
    try:
        record.write_text(
            json.dumps(
                {"pid": child.pid, "start": fingerprint(child.pid), "descriptor": lifetime.fileno()}
            )
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise RuntimeError(f"Gateway exited during startup; inspect {log}")
            try:
                with socket.create_connection((address, port), timeout=0.2):
                    print(f"Gateway listening on {address}:{port} (PID {child.pid}); log: {log}")
                    return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError(f"Gateway startup timed out; inspect {log}")
    except BaseException:
        child.terminate()
        child.wait(timeout=10)
        record.unlink(missing_ok=True)
        raise


def manage(action: str, state: Path) -> None:
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    record = state / "process.json"
    with (state / "control.lock").open("a+") as control:
        if not acquire(control):
            raise RuntimeError("Another gateway lifecycle command is in progress; retry shortly")
        with (state / "process.lock").open("a+") as lifetime:
            if acquire(lifetime):
                record.unlink(missing_ok=True)
                if action == "run":
                    start_gateway(state, record, lifetime)
                    return
                print("Gateway is stopped")
                return
            pid, handle = identify(record, lifetime)
            try:
                if action != "stop":
                    print(f"Gateway is running (PID {pid}); log: {state / 'gateway.log'}")
                    return
                terminate_process(handle)
                if not select.select([handle], [], [], 15)[0]:
                    raise RuntimeError("Gateway did not stop within 15 seconds; no force-kill sent")
                record.unlink(missing_ok=True)
                print("Gateway stopped")
            finally:
                os.close(handle)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run", "stop", "status"))
    parser.add_argument("state", type=Path)
    args = parser.parse_args()
    if not hasattr(NATIVE, "pidfd_open") or not hasattr(NATIVE, "pidfd_send_signal"):
        parser.exit(1, "Gateway run/stop requires Linux PID file-descriptor support\n")
    os.umask(0o077)
    try:
        manage(args.action, args.state.resolve())
    except ValidationError:
        parser.exit(
            1, "Invalid gateway configuration; check your environment/.env and auth registry\n"
        )
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
