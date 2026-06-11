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
- Python 3.11+ with `websockets` and `click`
- `bubblewrap` (only for `--local` mode)

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
claude-isol                         # run Claude Code
claude-isol "fix the bug"           # run with a prompt
claude-isol --shell                 # drop into bash inside the container
                                    # (handy for `gh auth login` etc.)
claude-isol --image NAME            # run a prebuilt image as-is
claude-isol --local                 # run on the host in a bubblewrap sandbox
claude-isol --tmpfs-home            # mount HOME as a fresh tmpfs (ephemeral home)
claude-isol -v /data:/data          # add extra mounts (repeatable, both modes)
claude-isol -- -p "hi" --model opus # forward flags through to claude after `--`
```

Unknown options are **rejected** (so a typo'd flag is caught, not silently
forwarded). To pass options through to `claude`, put them after a `--`
separator; a bare prompt needs no separator. Run `claude-isol --help` for the
full flag list.

With no `--image`, `claude-isolation:latest` is used and built from the
bundled Dockerfile on first run if it isn't present (override the default
tag with `CLAUDE_ISO_IMAGE`). With `--image NAME`, that image is run as-is
and never built — bring your own.

`--tmpfs-home` mounts the container's HOME as a fresh tmpfs, so anything the
image ships under the home dir is wiped and nothing written there persists; the
`~/.claude`, `~/.claude.json` and scoped `gh`/`git` config are still bind-mounted
on top, so authentication survives. (Under `--local` the home is always a tmpfs,
so the flag is a no-op there.)

## Local sandbox mode (`--local`)

`claude-isol --local` skips podman entirely and runs the host's own Claude Code
under a [bubblewrap](https://github.com/containers/bubblewrap) sandbox — handy when
you don't want to build or run a container. It requires the `bubblewrap` package
(`bwrap`).

What it exposes:

- Only the minimal system dirs needed to run, all **read-only**: `/usr`, `/etc`,
  `/opt`, `/usr/local`, and the usr-merge compat links (`/bin`, `/lib`, …). The
  layout is probed at runtime, so it works on Arch as well as distros where those
  are real directories (e.g. some openSUSE setups).
- The **current working directory** is the single read-write tree.
- `HOME` is a fresh ephemeral tmpfs; only `~/.claude` and `~/.claude.json` are
  bound in (writable), plus the scoped `~/.config/gh-claude` / `~/.gitconfig-claude`
  credentials (same indirection as container mode). Nothing else from your home
  (ssh keys, other projects) is visible.
- Networking is shared with the host (Claude needs it to reach the API), and **all
  host environment variables are forwarded** unchanged.
- Extra `-v src:dst[:ro]` mounts are honored (translated to bwrap binds).

`--local --shell` drops into your shell (`$SHELL`, falling back to bash) inside the
same sandbox (handy for inspecting what's exposed, or `gh auth login` against the
scoped credentials).

The JetBrains MCP proxy and host notifications are container-only and not wired up
in `--local` mode.

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