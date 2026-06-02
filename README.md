# claude-isol

Run [Claude Code](https://claude.com/claude-code) inside an isolated podman
container, with an MCP-filtering proxy in front of the JetBrains IDE link.

> **Disclaimer:** This is an unofficial, community project. It is not
> affiliated with, endorsed by, or supported by Anthropic. "Claude" and
> "Claude Code" are trademarks of Anthropic. Use at your own risk.

## What it does

- Launches Claude Code in an Arch Linux container, mounting only the current
  working directory and the user's `~/.claude` config.
- Detects when a JetBrains IDE has registered an MCP endpoint for the current
  workspace (via `~/.claude/ide/*.lock`) and transparently routes the
  container's traffic through the bundled MCP proxy.
- The proxy whitelists a small set of IDE tools (`getDiagnostics`, `openDiff`,
  `close_tab`, `closeAllDiffTabs`) and rejects everything else, so the
  containerized Claude cannot reach back out through the IDE.
- Runs as the host user inside the container via `--userns=keep-id`, with
  passwordless `sudo` available.
- Uses container-scoped credentials by bind-mounting `~/.config/gh-claude`
  to `~/.config/gh` and `~/.gitconfig-claude` to `~/.gitconfig` (both are
  created on first run if missing) so `gh` and `git` inside don't see the
  host's credentials.
- Optionally posts a host desktop notification (one line per running session,
  with a state icon) so you can see at a glance which container is busy, blocked
  on you, or done — see [Host notifications](#host-notifications).

## Components

| File | Role |
| --- | --- |
| `claude_isol/claude_isol.py` | Entry point. Spawns the proxy (if needed) and exec's `podman run`. |
| `claude_isol/mcp_proxy.py` | Filtering WebSocket proxy in front of the IDE's MCP endpoint. |
| `claude_isol/Dockerfile` | Arch-based image with `claude-code` installed from `arch-pre-built`. |
| `claude_isol/notifyd.py` | Host notification daemon (`claude-isol-notifyd`); renders the status board over D-Bus. |
| `claude_isol/notify_client.py` | In-container hook forwarder that reports session state to the daemon. |
| `pyproject.toml` | Python package definition (`pip install` entry point). |
| `PKGBUILD` | Arch package definition for `claude-isol`. |

## Requirements

- `podman` (with `pasta` networking)
- Python 3.11+ with `websockets`

## Installing

User-local with pip:

```sh
pip install --user .
```

System-wide via the Arch package:

```sh
makepkg -f
sudo pacman -U claude-isol-*.pkg.tar.zst
```

## Usage

```sh
claude-isol [claude args...]        # run Claude Code
claude-isol --shell                 # drop into bash inside the container
                                    # (handy for `gh auth login` etc.)
claude-isol --image NAME [args...]  # run a prebuilt image as-is
claude-isol -v /data:/data [args]   # add extra mounts (repeatable)
```

With no `--image`, `claude-isolation:latest` is used and built from the
bundled Dockerfile on first run if it isn't present (override the default
tag with `CLAUDE_ISO_IMAGE`). With `--image NAME`, that image is run as-is
and never built — bring your own.

## Host notifications

A small host daemon (`claude-isol-notifyd`) can show a single desktop
notification that acts as a status board for every running session — one line
per instance, updated in place:

- ⏳ working
- ❓ blocked on you (permission or idle prompt) — re-alerts
- ✅ finished / ready — re-alerts on completion

It uses the session D-Bus `org.freedesktop.Notifications` interface (via
`gdbus`). The container side never touches the host bus: each Claude session
forwards its hook events to the daemon over a single-purpose Unix socket under
`$XDG_RUNTIME_DIR/claude-isol/`, and `claude-isol` injects the reporting hooks
only into the containerized session (via `claude --settings`), so the host's own
Claude config is untouched.

Enable it once as a `systemd --user` service:

```sh
# pip install: write a unit pointing at the installed binary, then enable it
claude-isol-notifyd --install-user-unit
systemctl --user enable --now claude-isol-notifyd

# Arch package: the unit ships in /usr/lib/systemd/user
systemctl --user enable --now claude-isol-notifyd
```

After that, `claude-isol` auto-detects the daemon's socket and wires up
notifications; with the daemon stopped it does nothing. `claude-isol-notifyd
--reset` clears the board (e.g. after a container was killed without exiting
cleanly). Notifications require `python3` inside the image (the bundled image
has it) and a running notification server on the host.