#!/usr/bin/env bash
# Test mirror of Build-Chain.ps1: adjacent-only chain + optional endpoint diff.
set -uo pipefail
VERSIONS=("$@"); MODULES="Init Lean"
MODERN=EnvSnapshot.lean; LEGACY=EnvSnapshot_legacy.lean; DIFFER=EnvDiff.lean
SNAP=snapshots; CAUSE=causes; mkdir -p "$SNAP" "$CAUSE"
ORIGPATH="$PATH"
bindir() { case "$1" in
  4.16.0) echo "/home/claude/toolchain/lean-4.16.0-linux/bin" ;;
  4.24.0) echo "/home/claude/tc24/lean-4.24.0-linux/bin" ;;
  4.32.0) echo "/home/claude/tc32/lean-4.32.0-linux/bin" ;;
  *) echo "" ;; esac; }

OK=()
for v in "${VERSIONS[@]}"; do
  snap="$SNAP/snap_$v.ndjson"
  if [[ -s "$snap" ]]; then echo "[$v] cached"; OK+=("$v"); continue; fi
  BIN=$(bindir "$v"); [[ -z "$BIN" ]] && { echo "[$v] no toolchain"; continue; }
  used=""
  for d in "$MODERN" "$LEGACY"; do
    PATH="$BIN:$ORIGPATH" lean --run "$d" "$snap" $MODULES >"$SNAP/log_$v.txt" 2>&1
    [[ $? -eq 0 ]] && { used="$d"; break; }
  done
  [[ -n "$used" ]] && { echo "[$v] OK via $used"; OK+=("$v"); } || echo "[$v] FAILED"
done

DBIN=$(bindir "${OK[-1]}")
runpair() { # base target label
  local out="$CAUSE/${1}__${2}.ndjson"
  PATH="$DBIN:$ORIGPATH" lean --run "$DIFFER" "$SNAP/snap_$1.ndjson" "$SNAP/snap_$2.ndjson" "$out" >/dev/null 2>&1
  echo "$(grep -c '"sev":"break"' "$out")"
}

echo "=== CHAIN (adjacent hops only) ==="
printf 'from,to,breaks\n' > chain.csv
for ((i=0;i<${#OK[@]}-1;i++)); do
  b=${OK[$i]}; t=${OK[$i+1]}; n=$(runpair "$b" "$t")
  printf '%s,%s,%s\n' "$b" "$t" "$n" >> chain.csv
  printf '  %-8s -> %-8s : %s breaks\n' "$b" "$t" "$n"
done

if [[ "${WITH_ENDPOINTS:-0}" == "1" ]]; then
  b=${OK[0]}; t=${OK[-1]}; n=$(runpair "$b" "$t")
  printf '%s,%s,%s\n' "$b" "$t" "$n" >> chain.csv
  echo "=== ENDPOINT (the big jump, 1 extra diff) ==="
  printf '  %-8s -> %-8s : %s breaks\n' "$b" "$t" "$n"
fi
echo "--- chain.csv ---"; cat chain.csv
