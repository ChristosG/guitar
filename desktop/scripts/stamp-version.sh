#!/usr/bin/env bash
# Put the release version where every part of the build can see it, and print
# it on stdout for the caller.
#
# WHY THIS EXISTS. `tauri.conf.json` and `Cargo.toml` both carried a hardcoded
# 0.1.0, so the `desktop-v0.2.0` tag shipped `GuitarTutor_0.1.0_amd64.deb`.
# Two things followed, and the second is the one that actually hurt:
#
#   * nothing on the tutor's machine could say which build he was running —
#     `install-state.json` and `meta.json` both record 0.1.0 from
#     `env!("CARGO_PKG_VERSION")`;
#   * `apt install ./file.deb` compares versions, saw no increase, and left the
#     OLD app installed. That is the whole "do I have to uninstall it first?"
#     symptom, and it was a one-line cause.
#
# ONE SOURCE OF TRUTH: `tauri.conf.json` deliberately carries NO `version`
# field — Tauri falls back to Cargo.toml for a Rust project — so this single
# line drives the bundle version, the artifact filename, the deb control
# version AND the app's own self-report. They cannot drift.
#
# ONE SCRIPT FOR BOTH RUNNERS on purpose: the obvious inline `sed -i` is not
# portable (GNU takes `-i`, BSD/macOS demands `-i ''`, and `0,/re/` addressing
# is GNU-only), so writing it twice means writing it differently twice, and the
# macOS half would be the one nobody notices is broken.
#
# Usage: stamp-version.sh [ref]   (defaults to $GITHUB_REF)
set -euo pipefail

CARGO_TOML="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/src-tauri/Cargo.toml"
[ -f "$CARGO_TOML" ] || { echo "no Cargo.toml at $CARGO_TOML" >&2; exit 1; }

current_version() {
  grep -m1 -E '^version = ' "$CARGO_TOML" | sed -E 's/^version = "(.*)"$/\1/'
}

REF="${1:-${GITHUB_REF:-}}"

case "$REF" in
  refs/tags/desktop-v*)
    V="${REF#refs/tags/desktop-v}"
    # VALIDATED, NOT TRUSTED. A typo'd tag would otherwise produce a bundle
    # with a malformed version, and dpkg's complaint about that arrives far
    # from the cause. It is also what keeps a tag name out of the substitution
    # below as anything but three integers.
    printf '%s' "$V" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$' \
      || { echo "tag version '$V' is not X.Y.Z" >&2; exit 1; }
    python3 - "$CARGO_TOML" "$V" <<'PY'
import re, sys
path, version = sys.argv[1], sys.argv[2]
src = open(path, encoding="utf-8").read()
# `count=1` and an anchored match: [package].version is the first `version =`
# at the start of a line, and a dependency pinned as `version = "2"` on its own
# line must never be the one that moves.
out, n = re.subn(r'(?m)^version = ".*"$', 'version = "%s"' % version, src, count=1)
if n != 1:
    sys.exit("could not find a [package] version line in %s" % path)
open(path, "w", encoding="utf-8").write(out)
PY
    echo "release build from ${REF}: version ${V}" >&2
    ;;
  *)
    # An untagged build is not a release, and it deliberately keeps whatever
    # Cargo.toml already says. Inventing something here would either collide
    # with a real version or sort BELOW the installed release, and apt refuses
    # a downgrade — turning every dev build into a manual uninstall.
    V="$(current_version)"
    echo "untagged build (${REF:-no ref}): leaving version at ${V}" >&2
    ;;
esac

printf '%s\n' "$V"
