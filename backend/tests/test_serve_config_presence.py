"""Exercise startup config admission before real optional dependency detection."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from support.shell import require_script_bash

REPO_ROOT = Path(__file__).resolve().parents[2]

# Keep the complete launcher and detector, replacing process/service operations.
_SERVE_STUBS = """
git() { return 1; }
lsof() { return 1; }
pgrep() { return 1; }
nginx() { return 0; }
sleep() { return 0; }
python3() { "$TEST_PYTHON" "$@"; }
env() {
    if [ "$1" = python3 ]; then shift; python3 "$@"; else command env "$@"; fi
}
"""

# Stop at dependency synchronization, before any service can be launched.
_PRELUDE = (
    _SERVE_STUBS
    + """
uv() {
    if [ "$1" = run ]; then cat >/dev/null; return "${TEST_UV_RUN_STATUS:-0}"; fi
    printf '%s\\n' "$@" > "$TEST_UV_ARGS"
    return 73
}
source "$1" "$2"
"""
)


def _prepare_script_checkout(checkout: Path) -> Path:
    scripts = checkout / "scripts"
    scripts.mkdir(parents=True)
    (checkout / "backend").mkdir()
    for name in ("serve.sh", "dotenv_exports.py", "detect_uv_extras.py"):
        shutil.copy2(REPO_ROOT / "scripts" / name, scripts / name)
    (scripts / "nginx-local-conf.sh").write_text("printf 'test-nginx.conf\\n'\n", encoding="utf-8")
    (scripts / "cleanup-containers.sh").write_text("exit 0\n", encoding="utf-8")
    return scripts


def _isolated_serve_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in (
        "DEER_FLOW_CONFIG_PATH",
        "DEER_FLOW_PROJECT_ROOT",
        "DEER_FLOW_HOME",
        "DEER_FLOW_ENV_FILE",
        "DEER_FLOW_STREAM_BRIDGE_REDIS_URL",
        "DEER_FLOW_SANDBOX_OWNERSHIP_REDIS_URL",
        "UV_EXTRAS",
        "BASH_ENV",
        "ENV",
    ):
        env.pop(key, None)
    return env


@pytest.mark.parametrize("mode", ["--dev", "--prod"])
@pytest.mark.parametrize("config_source", ["project-env", "project-dotenv", "explicit", "checkout", "legacy", "missing", "project-directory"])
def test_serve_config_presence_reaches_dependency_sync(tmp_path: Path, mode: str, config_source: str) -> None:
    bash = require_script_bash()
    checkout = tmp_path / "checkout"
    scripts = _prepare_script_checkout(checkout)
    (scripts / "config-upgrade.sh").write_text('printf upgraded > "$TEST_UPGRADE_MARKER"\n', encoding="utf-8")

    env = _isolated_serve_env()
    project = tmp_path / "external project"
    project.mkdir()
    env["DEER_FLOW_PROJECT_ROOT"] = project.as_posix()
    expected_extras = []
    if config_source in {"project-env", "project-dotenv"}:
        (project / "config.yaml").write_text("models:\n  - use: langchain_ollama:ChatOllama\n", encoding="utf-8")
        expected_extras = ["--extra", "ollama"]
        if config_source == "project-dotenv":
            env.pop("DEER_FLOW_PROJECT_ROOT")
            (checkout / ".env").write_text(f"DEER_FLOW_PROJECT_ROOT={shlex.quote(project.as_posix())}\n", encoding="utf-8")
    elif config_source == "project-directory":
        (project / "config.yaml").mkdir()
    elif config_source != "missing":
        config = {"explicit": tmp_path / "explicit.yaml", "checkout": checkout / "config.yaml", "legacy": checkout / "backend" / "config.yaml"}[config_source]
        config.write_text("database:\n  backend: sqlite\n", encoding="utf-8")
        if config_source == "explicit":
            env["DEER_FLOW_CONFIG_PATH"] = config.as_posix()

    marker = tmp_path / "upgrade.txt"
    uv_args = tmp_path / "uv-args.txt"
    env.update(TEST_PYTHON=Path(sys.executable).as_posix(), TEST_UPGRADE_MARKER=marker.as_posix(), TEST_UV_ARGS=uv_args.as_posix())
    result = subprocess.run(
        [bash, "-c", _PRELUDE, "startup-config-test", (scripts / "serve.sh").as_posix(), mode],
        cwd=checkout,
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )

    assert result.returncode == 1, result.stdout + result.stderr
    if config_source in {"missing", "project-directory"}:
        assert "No DeerFlow config file found" in result.stdout
        assert not marker.exists()
        assert not uv_args.exists()
    else:
        assert "No DeerFlow config file found" not in result.stdout, result.stdout + result.stderr
        assert marker.read_text(encoding="utf-8") == "upgraded"
        assert uv_args.read_text(encoding="utf-8").splitlines() == ["sync", "--locked", "--quiet", "--all-packages", *expected_extras]
        assert "Backend dependency install failed" in result.stdout


@pytest.mark.parametrize("environment_available", [True, False])
def test_serve_reruns_config_upgrade_after_dependency_install(tmp_path: Path, environment_available: bool) -> None:
    bash = require_script_bash()
    checkout = tmp_path / "checkout"
    scripts = _prepare_script_checkout(checkout)
    (checkout / "frontend").mkdir()
    (scripts / "config-upgrade.sh").write_text(
        'printf \'upgrade\\n\' >> "$TEST_EVENTS"\nif [ "$(grep -c \'^upgrade$\' "$TEST_EVENTS")" -eq "$TEST_UPGRADE_CALLS_TO_EXIT" ]; then exit 42; fi\n',
        encoding="utf-8",
    )
    (scripts / "pnpm.py").write_text(
        "import os\nwith open(os.environ['TEST_EVENTS'], 'a', encoding='utf-8') as stream:\n    stream.write('frontend-install\\n')\n",
        encoding="utf-8",
    )
    (checkout / "config.yaml").write_text("database:\n  backend: sqlite\n", encoding="utf-8")

    events = tmp_path / "events.txt"
    env = _isolated_serve_env()
    env.update(
        TEST_PYTHON=Path(sys.executable).as_posix(),
        TEST_EVENTS=events.as_posix(),
        TEST_UV_RUN_STATUS="0" if environment_available else "73",
        TEST_UPGRADE_CALLS_TO_EXIT="2" if environment_available else "1",
    )
    prelude = (
        _SERVE_STUBS
        + """
uv() {
    if [ "$1" = run ]; then
        cat >/dev/null
        printf 'probe\\n' >> "$TEST_EVENTS"
        return "$TEST_UV_RUN_STATUS"
    fi
    if [ "$1" = sync ]; then printf 'sync\\n' >> "$TEST_EVENTS"; else printf '%s\\n' "$*" >> "$TEST_EVENTS"; fi
    return 0
}
source "$1" "$2"
"""
    )
    result = subprocess.run(
        [bash, "-c", prelude, "startup-config-test", (scripts / "serve.sh").as_posix(), "--dev"],
        cwd=checkout,
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )

    assert result.returncode == 42, result.stdout + result.stderr
    expected = ["probe"]
    if environment_available:
        expected.append("upgrade")
    expected.extend(["sync", "frontend-install", "upgrade"])
    assert events.read_text(encoding="utf-8").splitlines() == expected
    if not environment_available:
        assert "deferring the config upgrade" in result.stdout
