#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  bash ./scripts/live_opencode_v2_session_smoke.sh

Purpose:
  Start an isolated, password-protected OpenCode 2.0.28 server and exercise
  the application-selected V2 adapter's /api/info, /api/location, and session
  create/get/list routes. This smoke does not submit a model prompt.

Optional environment:
  OPENCODE_BIN             OpenCode executable (default: opencode from PATH)
EOF
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi

if [[ "$#" -gt 0 ]]; then
  echo "Expected no positional arguments" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
opencode_bin="${OPENCODE_BIN:-opencode}"
if ! command -v "${opencode_bin}" >/dev/null 2>&1; then
  echo "OpenCode CLI not found: ${opencode_bin}" >&2
  exit 1
fi
for required_command in curl python3 uv; do
  if ! command -v "${required_command}" >/dev/null 2>&1; then
    echo "${required_command} not found in PATH" >&2
    exit 1
  fi
done

opencode_version="$("${opencode_bin}" --version 2>/dev/null | tr -d '[:space:]')"
if [[ "${opencode_version}" != "opencodev2.0.28" ]]; then
  echo "Expected OpenCode 2.0.28; found ${opencode_version}" >&2
  exit 1
fi

free_port() {
  python3 - <<'PY'
import socket

with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
}

run_dir="$(mktemp -d "${TMPDIR:-/tmp}/opencode-a2a-v2-session.XXXXXX")"
workspace_dir="${run_dir}/workspace"
home_dir="${run_dir}/home"
mkdir -p "${workspace_dir}" "${home_dir}/.config" "${home_dir}/.local/share" "${home_dir}/.cache"
upstream_port="$(free_port)"
upstream_url="http://127.0.0.1:${upstream_port}"
upstream_username="opencode"
read -r upstream_password inbound_token < <(
  python3 -c 'import secrets; print(secrets.token_urlsafe(32), secrets.token_urlsafe(32))'
)
upstream_log="${run_dir}/opencode.log"
server_pid=""

cleanup() {
  local exit_code="$1"
  if [[ -n "${server_pid}" ]] && kill -0 "${server_pid}" >/dev/null 2>&1; then
    kill "${server_pid}" >/dev/null 2>&1 || true
    wait "${server_pid}" >/dev/null 2>&1 || true
  fi
  rm -rf "${run_dir}"
  exit "${exit_code}"
}
trap 'cleanup $?' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

echo "[v2-session-smoke] starting OpenCode 2.0.28 on an ephemeral localhost port"
env -i \
  PATH="${PATH}" \
  HOME="${home_dir}" \
  XDG_CONFIG_HOME="${home_dir}/.config" \
  XDG_DATA_HOME="${home_dir}/.local/share" \
  XDG_CACHE_HOME="${home_dir}/.cache" \
  TMPDIR="${run_dir}" \
  OPENCODE_SERVER_USERNAME="${upstream_username}" \
  OPENCODE_SERVER_PASSWORD="${upstream_password}" \
  "${opencode_bin}" serve --hostname 127.0.0.1 --port "${upstream_port}" \
  >"${upstream_log}" 2>&1 &
server_pid="$!"

upstream_ready=""
for _ in $(seq 1 60); do
  if ! kill -0 "${server_pid}" >/dev/null 2>&1; then
    echo "[v2-session-smoke] OpenCode exited before becoming ready" >&2
    exit 1
  fi
  if curl -fsS --max-time 2 -u "${upstream_username}:${upstream_password}" \
    "${upstream_url}/api/info" >/dev/null 2>&1; then
    upstream_ready="1"
    break
  fi
  sleep 0.25
done
if [[ -z "${upstream_ready}" ]]; then
  echo "[v2-session-smoke] OpenCode did not become ready" >&2
  exit 1
fi

auth_status="$(curl -sS --max-time 2 -o /dev/null -w '%{http_code}' \
  -u "${upstream_username}:invalid-${inbound_token}" "${upstream_url}/api/info")"
if [[ "${auth_status}" != "401" ]]; then
  echo "[v2-session-smoke] expected invalid credentials to return 401; got ${auth_status}" >&2
  exit 1
fi

env -i \
  PATH="${PATH}" \
  HOME="${run_dir}/adapter-home" \
  TMPDIR="${run_dir}" \
  V2_SMOKE_BASE_URL="${upstream_url}" \
  V2_SMOKE_WORKSPACE="${workspace_dir}" \
  V2_SMOKE_USERNAME="${upstream_username}" \
  V2_SMOKE_PASSWORD="${upstream_password}" \
  V2_SMOKE_TOKEN="${inbound_token}" \
  uv run --project "${ROOT_DIR}" python - <<'PY'
import asyncio
import os
from pathlib import Path

from opencode_a2a.config import Settings, StaticAuthCredentialSettings
from opencode_a2a.server.application import create_app
from opencode_a2a.upstream.v2 import V2UpstreamAdapter


async def main() -> None:
    workspace = Path(os.environ["V2_SMOKE_WORKSPACE"])
    settings = Settings(
        _env_file=None,
        opencode_api_version="v2",
        opencode_base_url=os.environ["V2_SMOKE_BASE_URL"],
        opencode_workspace_root=str(workspace),
        opencode_auth_username=os.environ["V2_SMOKE_USERNAME"],
        opencode_auth_password=os.environ["V2_SMOKE_PASSWORD"],
        a2a_task_store_backend="memory",
        a2a_static_auth_credentials=(
            StaticAuthCredentialSettings(
                scheme="bearer",
                token=os.environ["V2_SMOKE_TOKEN"],
                principal="v2-session-smoke",
            ),
        ),
    )
    app = create_app(settings)
    upstream = app.state.upstream_client
    if not isinstance(upstream, V2UpstreamAdapter):
        raise RuntimeError("Application did not select the V2 upstream adapter")

    try:
        server_info = await upstream.get_server_info()
        if server_info.get("version") != "2.0.28":
            raise RuntimeError("V2 /api/info reported an unexpected version")
        location = await upstream.get_location(directory=str(workspace))
        session_id = await upstream.create_session("V2 session smoke", directory=str(workspace))
        session = await upstream.get_session(session_id, directory=str(workspace))
        sessions = await upstream.list_sessions(
            directory=str(workspace),
            params={"limit": 5},
        )
        if location.get("directory") != str(workspace):
            raise RuntimeError("V2 location did not preserve the selected workspace")
        if session.get("id") != session_id:
            raise RuntimeError("V2 session lookup returned a different session")
        if not any(item.get("id") == session_id for item in sessions.get("data", [])):
            raise RuntimeError("V2 session list did not include the created session")
    finally:
        await upstream.close()
        await app.state.a2a_client_manager.close_all()


asyncio.run(main())
PY

echo "[v2-session-smoke] Basic-auth rejection: passed"
echo "[v2-session-smoke] /api/info, /api/location, session create/get/list: passed"
echo "[v2-session-smoke] no model prompt was submitted"
