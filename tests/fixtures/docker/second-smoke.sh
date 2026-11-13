#!/bin/sh
# The second image's check in the Self-test: each line of second-smoke-command arrives as one argument, every
# capability is dropped, no new privileges, seccomp.json (which refuses mkdir) and a 128m /dev/shm.
fail() { echo "$1" >&2; exit 1; }
[ "$#" = 1 ] && [ "$1" = "two words" ] || fail "the arguments are not the command's lines: $*"
grep -q '^NoNewPrivs:[[:space:]]*1$' /proc/self/status || fail "no-new-privileges is off"
grep -q '^CapBnd:[[:space:]]*0000000000000000$' /proc/self/status || fail "a capability was not dropped"
if mkdir /tmp/refused 2> /dev/null; then fail "the seccomp profile is not applied"; fi
[ "$(df -k /dev/shm | awk 'NR == 2 { print $2 }')" = 131072 ] || fail "/dev/shm is not 128m"
echo "second image ok"
