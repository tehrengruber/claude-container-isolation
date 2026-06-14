// SPDX-License-Identifier: GPL-2.0
//
// Privileged attacher for claude-isol --no-lan.
//
//   lan_block_load <cgroup-dir>
//
// Attaches every program in lan_block.bpf.o (the cgroup_skb/egress filter) to
// <cgroup-dir>, filtering it and all descendants, then exits -- the cgroup keeps
// the program alive.
//
// Installed with file capabilities (cap_bpf,cap_net_admin+ep) so an unprivileged
// claude-isol can run it without sudo. Because that makes it privilege-bearing
// and reachable by any local user, it is deliberately strict:
//
//   * The BPF object is loaded from beside THIS binary (root-owned install dir),
//     never from a caller-supplied path -- otherwise this would be an "attach any
//     BPF program with elevated privileges" escalation.
//   * <cgroup-dir> must be a real cgroup2 directory owned by the REAL uid of the
//     caller -- so a caller can only filter their own cgroups, not arbitrary ones
//     (which would let them DoS other users' / system traffic). The check uses
//     getuid(), so it holds under setcap, setuid, or plain sudo alike.

#include <errno.h>
#include <fcntl.h>
#include <libgen.h>
#include <limits.h>
#include <linux/magic.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/vfs.h>
#include <unistd.h>

#include <bpf/libbpf.h>
#include <bpf/bpf.h>

// Path to lan_block.bpf.o, resolved next to this executable (both root-owned).
static int object_path(char *out, size_t outsz)
{
	char exe[PATH_MAX];
	ssize_t n = readlink("/proc/self/exe", exe, sizeof(exe) - 1);

	if (n < 0)
		return -1;
	exe[n] = '\0';
	snprintf(out, outsz, "%s/lan_block.bpf.o", dirname(exe));
	return 0;
}

// Open the cgroup dir and reject it unless it is a real cgroup2 directory owned
// by the caller's REAL uid. All checks run against the open fd (no TOCTOU window)
// and use getuid(), so they hold whether we are setcap, setuid, or under sudo.
static int open_cgroup(const char *arg)
{
	int fd = open(arg, O_RDONLY | O_DIRECTORY);
	if (fd < 0) {
		fprintf(stderr, "open %s: %s\n", arg, strerror(errno));
		return -1;
	}

	struct statfs sfs;
	if (fstatfs(fd, &sfs) || sfs.f_type != CGROUP2_SUPER_MAGIC) {
		fprintf(stderr, "%s: not a cgroup2 directory\n", arg);
		close(fd);
		return -1;
	}

	struct stat st;
	if (fstat(fd, &st)) {
		fprintf(stderr, "fstat %s: %s\n", arg, strerror(errno));
		close(fd);
		return -1;
	}
	if (st.st_uid != getuid()) {
		fprintf(stderr, "%s: not owned by caller (uid %u)\n", arg, getuid());
		close(fd);
		return -1;
	}
	return fd;
}

static int attach_all(struct bpf_object *obj, int cg_fd)
{
	struct bpf_program *prog;
	int attached = 0;

	bpf_object__for_each_program(prog, obj) {
		enum bpf_attach_type t = bpf_program__expected_attach_type(prog);

		if (bpf_prog_attach(bpf_program__fd(prog), cg_fd, t,
				    BPF_F_ALLOW_MULTI)) {
			fprintf(stderr, "attach %s: %s\n",
				bpf_program__name(prog), strerror(errno));
			return -1;
		}
		attached++;
	}
	return attached;
}

int main(int argc, char **argv)
{
	if (argc != 2) {
		fprintf(stderr, "usage: %s <cgroup-dir>\n", argv[0]);
		return 2;
	}

	char obj_path[PATH_MAX];
	if (object_path(obj_path, sizeof(obj_path))) {
		fprintf(stderr, "cannot locate lan_block.bpf.o\n");
		return 1;
	}

	int cg_fd = open_cgroup(argv[1]);
	if (cg_fd < 0)
		return 1;

	struct bpf_object *obj = bpf_object__open_file(obj_path, NULL);
	if (!obj) {
		fprintf(stderr, "open %s: %s\n", obj_path, strerror(errno));
		return 1;
	}
	if (bpf_object__load(obj)) {
		fprintf(stderr, "load %s: %s\n", obj_path, strerror(errno));
		return 1;
	}

	int n = attach_all(obj, cg_fd);
	if (n < 0)
		return 1;

	fprintf(stderr, "attached %d hooks to %s\n", n, argv[1]);
	return 0;
}