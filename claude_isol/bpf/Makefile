# Build the --no-lan egress filter and its loader.
#   make            -> build in place (./lan_block.bpf.o + ./lan_block_load)
#   make O=<dir>    -> write both artifacts into <dir> instead, keeping the source
#                      tree clean (the PKGBUILD points this at the top-level build/).
# Needs: clang (BPF target) and libbpf headers/lib. No bpftool required.
# Only the .bpf.o needs clang; the loader is ordinary code built with $(CC).
CLANG ?= clang
CC    ?= cc
O     ?= .

all: $(O)/lan_block.bpf.o $(O)/lan_block_load

$(O)/lan_block.bpf.o: lan_block.bpf.c | $(O)
	$(CLANG) -g -O2 -target bpf -c $< -o $@

$(O)/lan_block_load: lan_block_load.c | $(O)
	$(CC) -O2 -Wall $< -lbpf -o $@

$(O):
	mkdir -p $(O)

clean:
	rm -f $(O)/lan_block.bpf.o $(O)/lan_block_load

.PHONY: all clean
