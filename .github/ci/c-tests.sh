#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# CI: build and run every CMake "*-test" target, as tools/test-port.sh does locally, from the
# port's root. A target is either run and must pass, or is in not_run below with the reason.
# A new target is run without anyone listing it; a not_run entry that is no longer a target
# fails, so the list can't go stale. Run in MSYS2 CLANG64 after build.sh.
set -uo pipefail
cd -- "$(dirname -- "$0")/../.."

# Upstream's tests that can't run on Windows here (tools/test-baseline.txt in the tool repo
# lists the same seven as known failures). Nothing of the plugins branch may be added. One
# table holds both the reasons and the names the stale check walks (port-aef0eef-N1).
unlinked='upstream: undefined symbols when linked on Windows (runtime_memory_is_mapped, runtime_atlas_*, ...)'
no_data='upstream: needs a trophy data file as its argument'
declare -A not_run=(
    [camera-motion-test]=$unlinked
    [scene-resolution-test]=$unlinked
    [taa-shader-test]=$unlinked
    [upscaler-support-test]='upstream: uses POSIX setenv/unsetenv'
    [trophy-data-test]=$no_data
    [trophy-history-test]=$no_data
    [trophy-toast-test]=$no_data
)
# Fewer targets than this means the CMake setup lost tests, not a clean run (39 on 2026-11-06).
min_targets=39

annotate() {  # <title> <log>: the log's last 40 lines as one error annotation
    local text
    text=$(tail -40 "$2" | sed -e 's/%/%25/g' -e 's/\r//g')
    echo "::error title=$1::${text//$'\n'/%0A}"
}

logs=out/ci
mkdir -p "$logs"
if ! all=$(ninja -C out/gpu -t targets all 2>&1); then
    echo "::error title=no test targets::ninja -C out/gpu -t targets all failed: $all"
    exit 1
fi
targets=$(printf '%s\n' "$all" | grep -o -E '^[a-z0-9-]+-test(\.exe)?:' | sed -E 's/(\.exe)?://' | sort -u)
count=$(printf '%s\n' "$targets" | grep -c . || true)
failed=0
if (( count < min_targets )); then
    echo "::error title=too few test targets::found $count *-test targets, expected at least $min_targets"
    failed=1
fi
for t in "${!not_run[@]}"; do
    if ! printf '%s\n' "$targets" | grep -qx -- "$t"; then
        echo "::error title=stale not-run entry::$t is listed as not run but is no longer a target: take it out of .github/ci/c-tests.sh"
        failed=1
    fi
done
ran=0
for t in $targets; do
    if [[ -v not_run[$t] ]]; then
        echo "NOT RUN $t: ${not_run[$t]}"
        continue
    fi
    ran=$((ran + 1))
    if ! ninja -C out/gpu "$t" > "$logs/build-$t.log" 2>&1; then
        echo "BUILD FAILED $t"
        cat "$logs/build-$t.log"
        annotate "$t did not build" "$logs/build-$t.log"
        failed=1
        continue
    fi
    if timeout 300 "out/gpu/$t.exe" > "$logs/run-$t.log" 2>&1; then
        echo "PASS $t: $(tail -1 "$logs/run-$t.log")"
    else
        echo "FAIL $t"
        cat "$logs/run-$t.log"
        annotate "$t failed" "$logs/run-$t.log"
        failed=1
    fi
done
echo "$ran of $count test targets run"
exit $failed
