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
| `claude_isol/Dockerfile` | Arch-based image with `claude-code` installed from `arch-pre-built` (x86_64) or npm (arm64). |
| `claude_isol/notifyd.py` | Host notification daemon (`claude-isol-notifyd`); renders the status board over D-Bus. |
| `claude_isol/notify_client.py` | In-container hook forwarder that reports session state to the daemon. |
| `claude_isol/host_execd.py` | Host-side executor for `--host-exec`; prompts, then runs the command. |
| `claude_isol/host_mcp.py` | In-container MCP server exposing the `mcp__host__run` tool. |
| `claude_isol/launch.py` | What the CLI collected, and which flags belong to which mode. |
| `claude_isol/hostos/` | What differs between Linux (`linux.py`) and macOS (`darwin.py`): the backend for each mode, the flags the OS cannot support, and the Linux-only kernel machinery. |
| `claude_isol/backends/` | One module per backend, one `launch()` each: `container`, `vm`, and `--local` as `bwrap` (Linux) or `seatbelt` (macOS). |
| `claude_isol/vm/image.py` | Builds and caches the bootable disk for `--vm` from the container image. |
| `claude_isol/Dockerfile.vm` | Guest image: the container image plus a kernel, systemd and sshd. |
| `claude_isol/Dockerfile.archlinuxarm` | arm64 base image, built from the Arch Linux ARM tarball (verified against `archlinuxarm.asc`). |
| `pyproject.toml` | Python package definition (`pip install` entry point). |
| `PKGBUILD` | Arch package definition for `claude-isol`. |
| `Formula/claude-isol.rb` | Homebrew formula. |
| `tests/` | Smoke tests of the installed command, run by CI on Linux and macOS. |

## Requirements

- `podman` (with `pasta` networking)
- Python 3.11+ with `websockets` and `click`
- `bubblewrap` (only for `--local` mode)
- `qemu-base`, `virtiofsd`, `openssh`, `passt`, `bubblewrap` and access to
  `/dev/kvm` (only for `--vm`)

On macOS, container mode needs `podman` with a running `podman machine`, and
`--local` needs nothing beyond the system's own `sandbox-exec`. See
[macOS](#macos) for what does and doesn't carry over.

## Installing

With Homebrew (macOS, or Linux), from this repository's formula:

```sh
brew tap tehrengruber/claude-isol https://github.com/tehrengruber/claude-container-isolation
brew install --HEAD claude-isol
```

The formula builds from the head of `main`, since there is no tagged release
yet; `brew upgrade --fetch-HEAD claude-isol` picks up new commits.

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
claude-isol --local                 # run on the host in a sandbox (bubblewrap
                                    # on Linux, Seatbelt on macOS)
claude-isol --vm                    # run in a QEMU VM: root inside, host behind
                                    # the VM boundary (needs /dev/kvm)
claude-isol --tmpfs-home            # mount HOME as a fresh tmpfs (ephemeral home;
                                    # under --local, keeps HOME writable)
claude-isol -v /data:/data          # add extra mounts (repeatable, both modes)
claude-isol -- -p "hi" --model opus # forward flags through to claude after `--`
claude-isol --exec -- CMD ARG..     # run CMD in the sandbox instead of claude
claude-isol -e VAR[=VAL]            # pass env into the container (repeatable;
                                    # --local forwards the full env already)
claude-isol --tcp-forward PORT      # container localhost:PORT -> host localhost:PORT
                                    # (repeatable; --local shares the host net)
claude-isol --host-exec             # let claude run commands on the host, each one
                                    # confirmed in a dialog there (container mode)
claude-isol --prompt-file task.md   # opening prompt from a file, so it never
                                    # lands in a command line or shell history
claude-isol --tmpfs-home-subdirectory .cache,.npm
                                    # writable tmpfs at those paths inside HOME,
                                    # the rest of HOME left as it is
claude-isol --podman-arg=--memory=4g
                                    # pass a flag straight to `podman run`
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
on top, so authentication survives. Under `--local` the home is always a fresh
tmpfs, and there the flag means something else: it leaves that home **writable**
instead of read-only (see below).

### Passing flags to podman

Unknown options are rejected, which is deliberate but leaves no room for the many
`podman run` flags this wrapper doesn't wrap. `--podman-arg` is that room:

```sh
claude-isol --podman-arg=--memory=4g --podman-arg=--pids-limit=512
claude-isol --podman-arg=--cap-add --podman-arg=NET_ADMIN
claude-isol --podman-arg=--mount --podman-arg=type=bind,source=/a,target=/b
```

One argument per occurrence, repeatable, passed on exactly as the shell delivered
it — so quoting happens once and nothing here re-interprets the value. That is
why a flag and a separate value take two occurrences: batching them would need a
separator, and both candidates break on podman's own syntax, where commas are
data (`type=bind,source=…`) and a path may contain a space.

Use the `=` form (`--podman-arg=--memory=4g`, not `--podman-arg --memory=4g`) or
the value is taken for an option of `claude-isol` itself.

The flags are appended after everything the wrapper sets and just before the
image name, so they win where podman takes the last value — which also means they
can undo the isolation (`--privileged`, `-v /:/host`, `--network=host`). That is
the point of an escape hatch; nothing checks what you pass. Container mode only,
since `--local` runs bwrap rather than podman.

### Writable paths inside HOME

HOME is read-only in both modes. In container mode that comes for free: HOME is
the image's own directory, owned by root, with just those four config paths
bind-mounted on top. Under `--local` the same shape is built deliberately — HOME
is a fresh tmpfs that is remounted read-only once everything has been mounted
into it, so the config binds and the working directory stay writable while HOME
itself is not. The mount is read-only, not merely unwritable by mode, so nothing
in the sandbox can `chmod` its way out.

Either way, nothing else under HOME can even be created, so a tool that wants
`~/.cache`, `~/.npm` or `~/.local/share` fails outright.

`--tmpfs-home-subdirectory` opens up only the paths you name, as tmpfs mounts,
leaving the rest of HOME as it was:

```sh
claude-isol --tmpfs-home-subdirectory .cache,.npm
claude-isol --tmpfs-home-subdirectory .cache --tmpfs-home-subdirectory .local/share
```

Paths are relative to HOME (an absolute path inside HOME is accepted too; one
outside it is rejected — that is what `-v` is for). Each mount is writable by the
session and allows execution, matching `/tmp`, and its contents are discarded
when the session ends — so it is scratch space, not storage; use `-v` for
anything that has to survive. Works in both modes.

To go the other way and have a fully writable home, use `--tmpfs-home` under
`--local`.

### Prompts and `ps`

A command line is public: `/proc/<pid>/cmdline` is world-readable, so on a shared
machine anything passed as an argument can be read by every other user for as
long as the session runs — in the launcher's process, and again in claude's,
which is an ordinary host process in both modes.

A print-mode prompt is therefore handed to claude on **stdin** rather than on the
command line, and never reaches an argv at all:

```sh
claude-isol -- -p "something confidential"
```

The prompt is moved when it can be identified without guesswork: when it is the
only non-option argument, or when it directly follows `-p`/`--print`. Other
layouts are left as they are rather than risked, and a prompt the caller pipes in
is never overwritten.

An interactive session's opening prompt (`claude-isol "fix the bug"`) stays on the
command line: claude accepts that one only as an argument, and by then you have
typed it on your own command line anyway, where `ps` and the shell history can
both see it.

`--prompt-file` is the way around that — write the prompt to a file and it never
reaches a command line at all, yours included:

```sh
claude-isol --prompt-file task.md          # interactive, opening prompt from the file
claude-isol --prompt-file task.md -- -p    # print mode, same file
```

In print mode the file's text goes in on stdin, exactly as if you had typed it.
Interactively there is no such channel — the TUI owns stdin — so the file is
bound into the sandbox read-only and passed as `@path`, which claude expands
inline when it builds the message (no `Read` tool, no permission prompt). The
command line then carries the path and nothing else. Note that this makes the
opening message a file reference with the contents attached, rather than the text
as your own words, so a prompt that reads oddly when quoted may land differently.
When the caller is already piping something into a print-mode session, the
`@path` form is used there too so the pipe is left alone.

## IDE integration

When a JetBrains IDE has registered an MCP endpoint for the current workspace
(`~/.claude/ide/*.lock`), the launcher routes it through the filtering proxy: the
proxy binds a fresh port, the lock file is copied under that port's name into the
container's `~/.claude/ide`, and `pasta` forwards the port inward.

That port is also announced to Claude via `CLAUDE_CODE_SSE_PORT`, which is what
lets the container run in its own PID namespace. Left to find the endpoint on its
own, Claude vets a lock file by signal-probing the pid recorded inside it and
walking its process tree looking for that pid — questions only the host's PID
namespace can answer. Naming the port satisfies the lookup directly, so host
processes stay out of reach from inside the container.

## VM mode (`--vm`)

`claude-isol --vm` boots a QEMU virtual machine where claude is **root in its own
kernel** — free to run containers in whatever mode it likes, mount filesystems
and load modules — while the host sits behind the VM boundary rather than sharing
a kernel with it. More power inside, more protection outside.

```sh
claude-isol --vm                       # 8G, half the host's cores
claude-isol --vm --vm-memory 16G --vm-cpus 8
```

Requires `/dev/kvm` (join the `kvm` group), plus `qemu-base`, `virtiofsd`,
`passt`, `bubblewrap` and `openssh`.

Hardware virtualization is passed through when the host allows nesting
(`/sys/module/kvm_amd/parameters/nested`, or `kvm_intel` on Intel), so the
session can run VMs of its own — `claude-isol --vm` included. The guest image
ships no qemu, so install what you want to run in there, or add it to
`Dockerfile.vm`. The first run builds a bootable disk from the container image and
caches it under `~/.cache/claude-isol/vm`, keyed by that image's digest, so it is
rebuilt only when the image changes.

**What the guest can see.** Three virtiofs shares and nothing else:

| share | host | guest |
| --- | --- | --- |
| `cwd` | the working directory | the same path as on the host, and where the session starts |
| `claude` | `~/.claude` | `/root/.claude` |
| `cfg` | a per-run staging dir | `/run/cfg`, read-only; the guest copies out of it |
| `vol<n>` | each `-v` source | wherever that `-v` asked for |

`-v src:dst[:ro]` works here too, one virtiofs share per source. Only
directories: virtiofs cannot share a single file, and sharing its parent instead
would hand the guest every sibling, so a file source is refused rather than
quietly widened.

virtiofsd maps guest uid 0 to the launching user, so claude is root inside the VM
and yet everything it writes to a share is owned by you on the host — the same
effect `--userns=keep-id` has in container mode. The scoped `gh`/`git`
credentials are copied into the staging directory, because virtiofs can share
directories but not individual files; `vm_init.sh` bind-mounts them into place
inside the guest.

**The console is an ssh session over vsock.** That is what makes window resizes,
signals, job control and the exit status behave: they are the ssh protocol's
job. Nothing is configured in the guest for it — `systemd-ssh-generator` binds
`vsock::22` by itself when it detects a VM and activates openssh's shipped
`sshd@.service` per connection, and `systemd-ssh-proxy` on the host turns
`ssh vsock/<cid>` into a vsock connect. The key is an ephemeral keypair per run,
its public half delivered as a systemd credential over SMBIOS. Agent forwarding
is explicitly off, so the VM never sees the host's keys.

**Networking.** The guest reaches the internet through `passt`, the same way the
container does — and with `--no-map-gw`, so it is *not* offered a route to the
host's own services the way QEMU's built-in user networking would be. `--no-lan`
works here too, and for the same reason it works for the container: passt
re-originates the guest's packets from this process tree, so running it under the
filtered cgroup puts every connection the guest makes behind the eBPF egress
filter. The guest's resolver is pinned to a public address when that is on, since
the host's own resolvers are usually in the private ranges the filter drops.

**Lifetime.** The session *is* the VM: `ssh` is exec'd in place, and QEMU plus
the three virtiofsd are forked children holding `PR_SET_PDEATHSIG`, so when the
ssh session ends they are torn down with it. The guest disk is opened with
`-snapshot`, so every write goes to a temporary overlay QEMU unlinks the moment
it creates it: the cached image is never written in place, concurrent VMs cannot
corrupt each other, and a half-written filesystem never survives a kill. The
per-run directory under `$XDG_RUNTIME_DIR/claude-isol` — holding the ephemeral
ssh key and the staged credentials — is removed by a small reaper that wakes on
our death; anything a SIGKILL leaves behind is swept on the next run.

Teardown is a hard stop rather than an ACPI shutdown, which is deliberate: the
only state that matters lives on the virtiofs shares, and those are write-through
(no `--writeback`), so the host's files are already current.

Not wired up in this mode: host notifications, `--host-exec` and the JetBrains
MCP proxy.

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
- `--dev-bind src:dst` (repeatable, `--local` only) binds a path **with device
  access**, so device nodes work inside the sandbox — e.g. `--dev-bind /dev/dri`
  for the GPU or `--dev-bind /dev/bus/usb` for USB. A plain `-v` bind blocks that.

`--local --shell` drops into your shell (`$SHELL`, falling back to bash) inside the
same sandbox (handy for inspecting what's exposed, or `gh auth login` against the
scoped credentials).

The JetBrains MCP proxy, host notifications and `--host-exec` are container-only
and not wired up in `--local` mode.

This is `--local` on Linux; on macOS it is built from Seatbelt instead (see
below).

## macOS

Container mode and `--local` both run on macOS. `--vm`, `--no-lan`,
`--host-exec`, `--tcp-forward` and `--dev-bind` don't, and are refused with an
error that says why.

**Container mode** runs through `podman machine`, so the container lives in
podman's Linux VM rather than on the Mac's own kernel:

```sh
brew install podman
podman machine init && podman machine start
claude-isol
```

The working directory has to be somewhere the machine shares in — by default
your home directory, `/private` and `/var/folders`. The bundled image is built
for the machine's own architecture. On Apple silicon that is arm64, where the
official Arch image does not exist, so the first run also builds a base image,
`claude-isolation-archlinuxarm`, from Arch Linux ARM's root filesystem tarball
(`Dockerfile.archlinuxarm`; about 800 MB to download, once). The tarball comes
over plain http, so the build checks its signature against the Arch Linux ARM
signing key shipped in `archlinuxarm.asc` before unpacking it. The image on top
skips the x86_64-only `arch-pre-built` repository and the packages that come
from it (`python310`–`python312`), and installs claude from npm instead.
Anything that would
hand the container a way back to the host is off, because none of it can cross
the VM: a Unix socket bind-mounted in arrives as a dead file, and pasta forwards
ports to the VM's loopback rather than the Mac's. So there is no JetBrains MCP
proxy, no host notifications, no `--host-exec` and no `--tcp-forward`.

The containerized claude cannot read the macOS keychain, which is where the
Mac's own claude keeps its login, so the first container session asks you to
log in. That login is written to `~/.claude/.credentials.json` and shared by the
container sessions after it.

**`--local`** runs the Mac's own claude under Seatbelt (`sandbox-exec`, part of
macOS). Seatbelt can't mount anything, only allow or deny access to the real
filesystem, so the bubblewrap layout is rebuilt from rules:

- `HOME` is a fresh per-run directory holding links to `~/.claude` and the
  scoped `gh`/`git` credentials, plus a throwaway copy of `~/.claude.json`. It
  is read-only (`--tmpfs-home-subdirectory` and `--tmpfs-home` open it up as on
  Linux), and `TMPDIR` is a fresh per-run directory too.
- Your real home directory can't be read, except the working directory, the
  links above, `-v` sources and the claude install.
- Nothing can be written outside the working directory, `~/.claude`, the scoped
  credentials, the per-run `TMPDIR`, `/private/tmp`, `/dev` and `-v` sources not
  marked `ro`. The `~/.claude` entries the host's claude would execute (the
  same list as on Linux) can't be written even when they don't exist yet.
- The environment is the same allowlist as on Linux, plus `-e`.
- Apple events and LaunchServices are blocked, so neither `osascript` nor `open`
  can start something outside the sandbox, and signals only reach processes
  inside it.
- `-v SRC[:ro]` makes `SRC` reachable where it is. Seatbelt can't move a path,
  so `-v SRC:DST` with a different `DST` is refused.

It is a weaker boundary than bubblewrap: the session shares the host's process
list, network and IPC, and reaches the same system services, the login keychain
included.

## Running on the host (`--host-exec`)

Sometimes a task needs something the sandbox does not have — a device, a VPN
route, a credential deliberately left outside. `--host-exec` gives the session a
single MCP tool, `mcp__host__run`, that runs one shell command **on the host,
outside the sandbox**, with `bash -lc` in the directory the session was launched
from.

Every call raises a confirmation dialog on the host showing the command
verbatim, and nothing runs unless you approve it there:

```sh
claude-isol --host-exec
```

The dialog is the whole point. It is drawn by a small host-side daemon
(`host_execd.py`) that owns the decision, so the gate holds even under
`--dangerously-skip-permissions`: the session can ask, and can ask often, but it
cannot answer. Requests are handled one at a time, and an unanswered prompt is
denied after five minutes.

Only the prompt is a boundary, not the transport. The socket is bind-mounted
into the container, so anything in there can reach the daemon directly rather
than going through the MCP tool — and it still lands in front of the same
dialog. What the tool adds is a readable name for the capability, not a lock.

The prompt is a `zenity` or `kdialog` dialog when either is installed, otherwise
a desktop notification with **Run on host** / **Deny** buttons over the same
D-Bus service the status board uses. If none of those can be reached — or the
notification server does not support buttons — the request is denied rather than
run unasked.

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
--reset` clears the board. Notifications require `python3` inside the image (the
bundled image has it) and a running notification server on the host.

The board keeps itself short. Whenever it changes, lines that no longer carry
information are dropped first: ones whose host process is gone (each event
carries the pid of the `podman` process that owns the session, so a container
killed without exiting cleanly doesn't linger) and ones silent for more than 5
minutes (`--idle-timeout SECONDS`, `0` disables). This bounds how long the board
gets; it is not a timer, so on a quiet board the last line stays until something
happens — dismiss the notification if it's in your way. A render that would
produce the exact same board as the last one is skipped, so an unchanged state
never pops the notification back up.