#!/usr/bin/env python3
"""
Walk the picked commits oldest -> newest. For each:
  checkout -> install toolchain -> `lake exe cache get` -> time it -> record failures.

This answers the *infrastructure* half of the feasibility question:
  - does the cache exist for arbitrary historical master commits?
  - how long does a checkout+cache-get cost once warm?
  - how much disk does 10 commits burn?

Outputs cache_probe.json (full detail) and cache_probe.csv (the table for your notes).

Usage:
  python3 20_probe_cache.py --repo ~/mathlib4 --candidates candidates.json
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_lib import (  # noqa: E402
    checkout, count_oleans, dir_size_bytes, gb, mathlib_cache_dir, run, tail, toolchain_of,
)

# `cache get` prints something like:
#   Attempting to download 5231 file(s)
#   Downloaded: 5231 file(s) [attempted 5231/5231 = 100%] (100% success)
RE_ATTEMPT = re.compile(r"Attempting to download (\d+) file")
RE_DOWNLOADED = re.compile(r"Downloaded:\s*(\d+) file\(s\).*?\((\d+)% success\)")
RE_MISSING = re.compile(r"(some files were not found in the cache|No cache files)", re.I)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--candidates", default="candidates.json")
    ap.add_argument("--out", default="cache_probe")
    ap.add_argument("--timeout", type=int, default=3600, help="seconds per cache get")
    ap.add_argument("--force", action="store_true", help="use `cache get!` (re-download)")
    args = ap.parse_args()

    repo = Path(args.repo).expanduser()
    cands = json.loads(Path(args.candidates).read_text(encoding="utf-8"))
    cdir = mathlib_cache_dir()

    print(f"[i] repo        : {repo}")
    print(f"[i] cache dir   : {cdir}")
    du = shutil.disk_usage(repo)
    print(f"[i] free disk   : {gb(du.free)} GB  (want >= 60 GB)")
    print(f"[i] commits     : {len(cands)}\n")

    rows = []
    for i, c in enumerate(cands, 1):
        sha = c["sha"]
        rec = {"i": i, "sha": sha, "pr": c.get("pr"), "class": c.get("class"),
               "subject": c.get("subject", "")}
        print(f"=== [{i}/{len(cands)}] {sha[:12]}  {rec['subject'][:60]}")

        checkout(repo, sha)
        tc = toolchain_of(repo)
        rec["toolchain"] = tc

        r_elan = run(["elan", "toolchain", "install", tc], timeout=1800)
        rec["elan_seconds"] = r_elan["seconds"]
        rec["elan_rc"] = r_elan["rc"]
        if r_elan["rc"] != 0:
            print(f"    [!] elan install rc={r_elan['rc']}: {tail(r_elan['stderr'], 300)}")

        cmd = ["lake", "exe", "cache", "get!" if args.force else "get"]
        r = run(cmd, cwd=repo, timeout=args.timeout)
        out = (r["stdout"] or "") + "\n" + (r["stderr"] or "")

        rec["cache_rc"] = r["rc"]
        rec["cache_seconds"] = r["seconds"]
        rec["cache_timed_out"] = r["timed_out"]
        m = RE_ATTEMPT.search(out)
        rec["attempted"] = int(m.group(1)) if m else None
        m = RE_DOWNLOADED.search(out)
        rec["downloaded"] = int(m.group(1)) if m else None
        rec["success_pct"] = int(m.group(2)) if m else None
        rec["missing_warning"] = bool(RE_MISSING.search(out))
        rec["oleans_on_disk"] = count_oleans(repo)
        rec["build_dir_gb"] = gb(dir_size_bytes(repo / ".lake" / "build"))
        rec["cache_dir_gb"] = gb(dir_size_bytes(cdir))
        rec["free_disk_gb"] = gb(shutil.disk_usage(repo).free)
        rec["stdout_tail"] = tail(out, 1500)

        status = "OK" if (r["rc"] == 0 and not rec["missing_warning"]) else "FAIL"
        print(f"    toolchain={tc}")
        print(f"    cache get -> {status} rc={r['rc']} in {r['seconds']}s "
              f"| downloaded={rec['downloaded']}/{rec['attempted']} "
              f"({rec['success_pct']}%) | oleans={rec['oleans_on_disk']}")
        print(f"    build={rec['build_dir_gb']}GB cache={rec['cache_dir_gb']}GB "
              f"free={rec['free_disk_gb']}GB")
        if status == "FAIL":
            print(f"    [!] {tail(out, 600)}")
        rows.append(rec)

        Path(args.out + ".json").write_text(json.dumps(rows, indent=2), encoding="utf-8")

    cols = ["i", "sha", "pr", "class", "toolchain", "cache_rc", "cache_seconds",
            "attempted", "downloaded", "success_pct", "missing_warning",
            "oleans_on_disk", "build_dir_gb", "cache_dir_gb", "free_disk_gb", "subject"]
    with open(args.out + ".csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    ok = [r for r in rows if r["cache_rc"] == 0 and not r["missing_warning"]]
    times = [r["cache_seconds"] for r in ok]
    print("\n================ SUMMARY ================")
    print(f"cache complete : {len(ok)}/{len(rows)} commits")
    if times:
        print(f"cache get time : min {min(times):.0f}s  median "
              f"{sorted(times)[len(times)//2]:.0f}s  max {max(times):.0f}s")
    print(f"wrote {args.out}.json / {args.out}.csv")
    for r in rows:
        if r["cache_rc"] != 0 or r["missing_warning"]:
            print(f"  FAILURE {r['sha'][:12]} rc={r['cache_rc']} "
                  f"missing={r['missing_warning']} timeout={r['cache_timed_out']}")


if __name__ == "__main__":
    main()
