#!/usr/bin/env bash
# Build and run the toolchain matrix.
#
#   ./containers/matrix.sh build v4.20.0 v4.21.0 v4.22.0
#   ./containers/matrix.sh sweep /corpus/out v4.20.0 v4.21.0 v4.22.0
#
# Note the asymmetry in cost, and exploit it: the expected-output drift pass needs
# no container at all (pure git plumbing, seconds per tag pair), while the package
# sweep needs a full elaboration of every package under every toolchain. Run drift
# first on the whole tag range, use it to pick which toolchains are worth a sweep,
# then sweep only those.
set -euo pipefail

MODE="${1:?usage: matrix.sh build|sweep|drift ...}"
shift

case "$MODE" in
  build)
    for TC in "$@"; do
      echo "==> building layer3/lean:${TC}"
      docker build -f containers/Dockerfile.toolchain \
        --build-arg "TOOLCHAIN=leanprover/lean4:${TC}" \
        -t "layer3/lean:${TC}" .
    done
    ;;

  drift)
    # No container. Requires a local clone of leanprover/lean4.
    REPO="${1:?repo path}"; shift
    OUT="${1:?output dir}"; shift
    mkdir -p "$OUT"
    prev=""
    for TC in "$@"; do
      if [ -n "$prev" ]; then
        echo "==> drift ${prev} -> ${TC}"
        python3 run.py drift --repo "$REPO" --from "$prev" --to "$TC" \
          > "${OUT}/drift-${prev}-${TC}.ndjson"
      fi
      prev="$TC"
    done
    ;;

  sweep)
    OUT="${1:?output dir}"; shift
    mkdir -p "$OUT"
    for TC in "$@"; do
      echo "==> sweep under ${TC}"
      # --network=none after fetch: a package build that can reach the network can
      # resolve a different dependency set than the one you recorded, and the run
      # stops being attributable to the toolchain.
      docker run --rm \
        -v "$(pwd)/corpus:/corpus:ro" \
        -v "$(realpath "$OUT"):/out" \
        --network=none \
        --cpus=4 --memory=16g \
        "layer3/lean:${TC}" \
        sweep --corpus /corpus --out "/out/obs-${TC}.ndjson" --toolchain "${TC}"
    done
    ;;

  *) echo "unknown mode: $MODE" >&2; exit 2 ;;
esac
