# cs2mmt-sentinel: plain C, libc only. `make`, `make test`, `make static` (needs a musl or
# glibc static toolchain; the Dockerfile builds a fully static binary for Synology).
CC ?= cc
CFLAGS ?= -O2 -g0
WARN = -std=c11 -Wall -Wextra -Wformat=2 -Wshadow -Wno-unused-parameter -D_FORTIFY_SOURCE=2 -fstack-protector-strong
LDFLAGS ?=
# Sanitizers for the unit tests (not available on musl/Alpine: `make test SAN=`).
SAN ?= -fsanitize=address,undefined
SRC = src/sentinel.c src/json.c src/util.c
HDR = src/json.h src/util.h

all: cs2mmt-sentinel

cs2mmt-sentinel: $(SRC) $(HDR)
	$(CC) $(CFLAGS) $(WARN) -o $@ $(SRC) $(LDFLAGS)

# Build next to the running binary and swap only on success (used by systemd's ExecStartPre).
rebuild: $(SRC) $(HDR)
	$(CC) $(CFLAGS) $(WARN) -o cs2mmt-sentinel.new $(SRC) $(LDFLAGS) && mv -f cs2mmt-sentinel.new cs2mmt-sentinel

static: $(SRC) $(HDR)
	$(CC) $(CFLAGS) $(WARN) -static -o cs2mmt-sentinel $(SRC) $(LDFLAGS)

tests/test_sentinel: tests/test_sentinel.c src/json.c src/util.c $(HDR)
	$(CC) -O1 -g $(WARN) $(SAN) -Isrc -o $@ tests/test_sentinel.c src/json.c src/util.c

test: tests/test_sentinel
	./tests/test_sentinel

clean:
	rm -f cs2mmt-sentinel tests/test_sentinel

.PHONY: all rebuild static test clean
