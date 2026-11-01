"""End-to-end checks of the installed `claude-isol`, run by CI on Linux and macOS.

They exercise the command on PATH -- on macOS, the one Homebrew installed -- with
HOME pointed at a scratch directory, so nothing touches the real one. --local is
run for real (bubblewrap on Linux, Seatbelt on macOS); container mode runs
against a stand-in `podman` that records its arguments, since CI has no podman
machine to start.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

MACOS = sys.platform == "darwin"

FAKE_PODMAN = """#!/bin/sh
case "$1" in
  image) exit 0 ;;
  run) printf '%s\\n' "$@" > "$PODMAN_LOG"; exit 0 ;;
esac
exit 1
"""


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    (h / ".claude").mkdir(parents=True)
    (h / ".claude" / "settings.json").write_text("{}")
    (h / ".claude.json").write_text("{}")
    (h / "secret").write_text("s3cret")
    return h


@pytest.fixture
def work(tmp_path: Path) -> Path:
    w = tmp_path / "work"
    w.mkdir()
    return w


def isol(*args: str, home: Path, cwd: Path, env: dict | None = None,
         path_prefix: Path | None = None) -> subprocess.CompletedProcess:
    full_env = {**os.environ, "HOME": str(home), **(env or {})}
    if path_prefix is not None:
        full_env["PATH"] = f"{path_prefix}{os.pathsep}{full_env['PATH']}"
    return subprocess.run(["claude-isol", *args], cwd=cwd, env=full_env,
                          stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, timeout=120)


def sh(script: str, *args: str) -> list[str]:
    return ["--local", "--exec", "--", "sh", "-c", script, "sh", *args]


def test_help(home, work):
    r = isol("--help", home=home, cwd=work)
    assert r.returncode == 0, r.stderr
    assert "Run Claude Code in isolation" in r.stdout


def test_local_writes_only_cwd(home, work, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    r = isol(*sh('echo ok > inside'), home=home, cwd=work)
    assert r.returncode == 0, r.stderr
    assert (work / "inside").read_text() == "ok\n"

    r = isol(*sh('echo leak > "$1"', str(outside / "f")), home=home, cwd=work)
    assert r.returncode != 0
    assert not (outside / "f").exists()


def test_local_hides_home(home, work):
    r = isol(*sh('cat "$1"', str(home / "secret")), home=home, cwd=work)
    assert r.returncode != 0
    assert "s3cret" not in r.stdout


def test_local_home_is_read_only_but_subdirs(home, work):
    r = isol(*sh('touch "$HOME/x"'), home=home, cwd=work)
    assert r.returncode != 0
    r = isol("--tmpfs-home-subdirectory", ".cache",
             *sh('touch "$HOME/.cache/x"'), home=home, cwd=work)
    assert r.returncode == 0, r.stderr


def test_local_pins_claude_config(home, work):
    r = isol(*sh('echo hook > "$HOME/.claude/settings.json"'), home=home, cwd=work)
    assert r.returncode != 0
    assert (home / ".claude" / "settings.json").read_text() == "{}"
    r = isol(*sh('echo t > "$HOME/.claude/transcript"'), home=home, cwd=work)
    assert r.returncode == 0, r.stderr
    assert (home / ".claude" / "transcript").read_text() == "t\n"


def test_local_scrubs_env(home, work):
    r = isol("-e", "FOO=bar", *sh('echo "${GH_TOKEN:-none} $FOO"'),
             home=home, cwd=work, env={"GH_TOKEN": "leak"})
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "none bar"


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude not installed")
def test_local_runs_claude(home, work):
    r = isol("--local", "--", "--version", home=home, cwd=work)
    assert r.returncode == 0, r.stderr
    assert "Claude Code" in r.stdout


@pytest.mark.skipif(not MACOS, reason="LaunchServices is macOS")
def test_local_cannot_launch_apps(home, work):
    r = isol("--local", "--exec", "--", "open", "-g", "-a", "TextEdit",
             home=home, cwd=work)
    assert r.returncode != 0


def test_container_argv(home, work, tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    podman = bin_dir / "podman"
    podman.write_text(FAKE_PODMAN)
    podman.chmod(0o755)
    log = tmp_path / "podman.log"

    r = isol("--exec", "--", "true", home=home, cwd=work,
             env={"PODMAN_LOG": str(log)}, path_prefix=bin_dir)
    assert r.returncode == 0, r.stderr
    argv = log.read_text().splitlines()
    assert "--userns=keep-id" in argv
    assert f"{work}:{work}" in argv
    assert argv[-1] == "true"


@pytest.mark.skipif(not MACOS, reason="macOS refuses these; Linux has them")
@pytest.mark.parametrize("args", [["--vm"], ["--no-lan"], ["--host-exec"],
                                  ["--tcp-forward", "8080"],
                                  ["--local", "--dev-bind", "/dev/null"]])
def test_macos_refuses_linux_only(home, work, args):
    r = isol(*args, home=home, cwd=work)
    assert r.returncode == 2
    assert "not available on macOS" in r.stderr
