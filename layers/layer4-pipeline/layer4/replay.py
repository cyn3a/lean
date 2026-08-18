"""Optional: turn *predicted* error classes into *observed* ones.

The mined labels are free because they come from the diff alone. Replay is the
paid step that validates them: check out the post-adaptation tree at
`toolchain_after`, apply the **reverse** edit to reintroduce the breakage, run
the elaborator on that one file, and capture the real diagnostics.

Requires a working `elan`/`lake` toolchain and a warm mathlib cache. Cost is
roughly one `lake env lean` per sample on an already-built dependency graph;
budget accordingly and sample rather than replaying everything.

Agreement between `expected_errors` and the observed class is the calibration
number to report for layer 4 -- it tells you which taxonomy rules are trusted
and which need the residual classifier.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, asdict, field
from pathlib import Path

from .taxonomy import parse_diagnostics, EXPECTED_ERRORS, ErrorClass


@dataclass
class ReplayResult:
    sample_id: str
    ok: bool
    applied: bool
    observed_errors: list[str] = field(default_factory=list)
    expected_errors: list[str] = field(default_factory=list)
    agreement: str = "not_run"        # match | mismatch | no_error | error
    diagnostics: list[dict] = field(default_factory=list)
    stderr: str = ""
    seconds: float = 0.0

    def to_json(self) -> dict:
        return asdict(self)


class ReplayEnv:
    """A checkout at a fixed commit, with a built dependency graph."""

    def __init__(self, repo: Path, commit: str, *, workdir: Path | None = None,
                 lake: str = "lake", build_deps: bool = True, timeout: int = 900):
        self.repo = Path(repo)
        self.commit = commit
        self.timeout = timeout
        self.lake = lake
        self.work = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="l4-replay-"))
        self._owned = workdir is None
        self.ready = False
        self.build_deps = build_deps

    def __enter__(self) -> "ReplayEnv":
        self.prepare()
        return self

    def __exit__(self, *exc) -> None:
        if self._owned:
            shutil.rmtree(self.work, ignore_errors=True)

    def prepare(self) -> None:
        self.work.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "--detach",
                        str(self.work), self.commit],
                       capture_output=True, timeout=self.timeout, check=True)
        if self.build_deps:
            subprocess.run([self.lake, "exe", "cache", "get"], cwd=self.work,
                           capture_output=True, timeout=self.timeout)
            subprocess.run([self.lake, "build", "--no-build"], cwd=self.work,
                           capture_output=True, timeout=self.timeout)
        self.ready = True

    def elaborate(self, rel_path: str) -> tuple[str, str, int]:
        env = dict(os.environ, LEAN_ABORT_ON_PANIC="1")
        proc = subprocess.run([self.lake, "env", "lean", rel_path],
                              cwd=self.work, capture_output=True,
                              timeout=self.timeout, env=env)
        return (proc.stdout.decode("utf-8", "replace"),
                proc.stderr.decode("utf-8", "replace"),
                proc.returncode)


def _apply_reverse_window(text: str, fixed_window: str, broken_window: str) -> str | None:
    if fixed_window and fixed_window in text:
        return text.replace(fixed_window, broken_window, 1)
    return None


def replay_pair(env: ReplayEnv, pair: dict) -> ReplayResult:
    import time
    t0 = time.time()
    sid = pair.get("sample_id", "")
    expected = pair.get("expected_errors") or sorted(
        EXPECTED_ERRORS.get(pair.get("label", ""), set()))
    rel = pair.get("path", "")
    target = env.work / rel
    if not target.exists():
        return ReplayResult(sid, False, False, expected_errors=expected,
                            agreement="error", stderr=f"missing file: {rel}")

    original = target.read_text(encoding="utf-8")
    broken = _apply_reverse_window(original, pair.get("fixed_window", ""),
                                   pair.get("broken_window", ""))
    if broken is None:
        return ReplayResult(sid, False, False, expected_errors=expected,
                            agreement="error",
                            stderr="fixed window not found at this commit")
    try:
        target.write_text(broken, encoding="utf-8")
        out, err, _rc = env.elaborate(rel)
    except subprocess.TimeoutExpired:
        return ReplayResult(sid, True, True, [ErrorClass.TIMEOUT], expected,
                            "match" if ErrorClass.TIMEOUT in expected else "mismatch",
                            seconds=time.time() - t0)
    finally:
        target.write_text(original, encoding="utf-8")

    diags = parse_diagnostics(out + "\n" + err)
    observed = sorted({d["error_class"] for d in diags if d["severity"] != "info"})
    if not observed:
        # a label that predicts no_error (formatting, adaptation note) is
        # *confirmed* by the absence of diagnostics
        agreement = "match" if ErrorClass.NONE in expected else "no_error"
    elif set(observed) & set(expected):
        agreement = "match"
    else:
        agreement = "mismatch"
    return ReplayResult(sid, True, True, observed, expected, agreement,
                        diagnostics=diags[:8], seconds=round(time.time() - t0, 2))


def replay(repo: Path, pairs: list[dict], *, commit: str | None = None,
           limit: int | None = None, progress=None, **env_kw) -> list[ReplayResult]:
    """Group pairs by commit so each checkout is prepared once."""
    from collections import defaultdict
    groups: dict[str, list[dict]] = defaultdict(list)
    for p in pairs:
        groups[commit or p.get("commit", "")].append(p)

    results: list[ReplayResult] = []
    budget = limit if limit is not None else 10 ** 9
    for sha, group in groups.items():
        if budget <= 0:
            break
        if not sha:
            continue
        with ReplayEnv(repo, sha, **env_kw) as env:
            for p in group[:budget]:
                r = replay_pair(env, p)
                results.append(r)
                budget -= 1
                if progress:
                    progress(f"  {r.sample_id} {r.agreement} "
                             f"{','.join(r.observed_errors) or '-'}")
    return results


def calibration(results: list[ReplayResult], pairs: list[dict]) -> dict:
    """Per-label agreement -- the trust score for each free label."""
    from collections import Counter, defaultdict
    by_id = {p.get("sample_id"): p for p in pairs}
    per: dict[str, Counter] = defaultdict(Counter)
    for r in results:
        label = by_id.get(r.sample_id, {}).get("label", "?")
        per[label][r.agreement] += 1
    out = {}
    for label, c in per.items():
        n = sum(c.values())
        out[label] = {
            "n": n,
            "match": c["match"], "mismatch": c["mismatch"],
            "no_error": c["no_error"], "error": c["error"],
            "precision": round(c["match"] / max(1, c["match"] + c["mismatch"]), 3),
            # no_error means the reverse edit did not actually break anything:
            # the pair is not a genuine repair instance and should be dropped.
            "genuine_break_rate": round(1 - c["no_error"] / max(1, n), 3),
        }
    return out
