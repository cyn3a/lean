"""Shared helpers for the Mathlib churn-repair feasibility probe.

Nothing here is Mathlib-version-specific except `DEFAULT_LEAN_OPTS`, which is only
used as a fallback if lakefile parsing fails. Everything is defensive on purpose:
the point of the probe is to *measure*, not to assume.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# --------------------------------------------------------------------------- #
# process running
# --------------------------------------------------------------------------- #

def run(cmd, cwd=None, timeout=None, env=None, label=None):
    """Run a command, always capture, always time, never raise on nonzero rc."""
    cmd = [str(c) for c in cmd]
    t0 = time.monotonic()
    timed_out = False
    try:
        p = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            timeout=timeout,
            capture_output=True,
            text=True,
            errors="replace",
            env=env,
        )
        rc, out, err = p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as e:
        rc, timed_out = -9, True
        out = _as_text(e.stdout)
        err = _as_text(e.stderr)
    except FileNotFoundError as e:
        rc, timed_out = -127, False
        out, err = "", f"command not found: {e}"
    dt = time.monotonic() - t0
    return {
        "label": label or " ".join(cmd),
        "cmd": cmd,
        "rc": rc,
        "seconds": round(dt, 2),
        "timed_out": timed_out,
        "stdout": out,
        "stderr": err,
    }


def _as_text(x):
    if x is None:
        return ""
    if isinstance(x, bytes):
        return x.decode("utf-8", "replace")
    return x


def tail(s, n=4000):
    s = s or ""
    return s if len(s) <= n else "...<truncated>...\n" + s[-n:]


# --------------------------------------------------------------------------- #
# git
# --------------------------------------------------------------------------- #

def git(repo, *args, check=True):
    r = run(["git", "-C", str(repo), *args])
    if check and r["rc"] != 0:
        raise RuntimeError(
            f"git {' '.join(str(a) for a in args)} failed (rc={r['rc']}):\n{tail(r['stderr'], 2000)}"
        )
    return r["stdout"]


def ensure_clean(repo):
    """Refuse to proceed if tracked files are dirty.

    NOTE: we deliberately ignore untracked files, because `.lake/` is gitignored
    and contains the multi-GB build output we care about.
    NEVER run `git clean -xfd` in this repo: it deletes .lake/build and you lose
    the entire cache download.
    """
    st = git(repo, "status", "--porcelain", "--untracked-files=no")
    if st.strip():
        raise SystemExit(
            f"[abort] tracked files are modified in {repo}:\n{st}\n"
            "Restore them (git checkout -- <path>) before running.\n"
            "Do NOT run `git clean -xfd` here: it would delete .lake/build."
        )


def checkout(repo, sha):
    ensure_clean(repo)
    git(repo, "checkout", "--detach", "--quiet", sha)


def toolchain_of(repo):
    return (Path(repo) / "lean-toolchain").read_text(encoding="utf-8").strip()


# --------------------------------------------------------------------------- #
# disk accounting
# --------------------------------------------------------------------------- #

def mathlib_cache_dir():
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "mathlib"
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "mathlib"


def dir_size_bytes(p):
    p = Path(p)
    if not p.exists():
        return 0
    total = 0
    for root, _dirs, files in os.walk(p, onerror=lambda e: None):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                pass
    return total


def count_oleans(repo):
    """Version-agnostic: newer Lake nests oleans under .lake/build/lib/lean/."""
    lib = Path(repo) / ".lake" / "build" / "lib"
    if not lib.exists():
        return 0
    n = 0
    for _root, _dirs, files in os.walk(lib):
        n += sum(1 for f in files if f.endswith(".olean"))
    return n


def olean_exists(repo, module):
    lib = Path(repo) / ".lake" / "build" / "lib"
    rel = module.replace(".", os.sep) + ".olean"
    for cand in (lib / rel, lib / "lean" / rel):
        if cand.exists():
            return True
    return False


def gb(nbytes):
    return round(nbytes / (1024 ** 3), 2)


# --------------------------------------------------------------------------- #
# Lean diagnostics
# --------------------------------------------------------------------------- #

def parse_lean_json(stdout):
    """Parse `lean --json` output: one JSON object per message, one per line."""
    msgs = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "severity" not in obj:
            continue
        pos = obj.get("pos") or {}
        end = obj.get("endPos") or {}
        msgs.append(
            {
                "severity": obj.get("severity"),
                "line": pos.get("line"),
                "col": pos.get("column"),
                "end_line": end.get("line"),
                "end_col": end.get("column"),
                "file": obj.get("fileName"),
                "text": obj.get("data", ""),
            }
        )
    return msgs


# `lean` prints  path:line:col: error: msg
# `lake` prints  error: path:line:col: msg
_RE_LEAN = re.compile(
    r"^(?P<path>\.{0,2}[^\s:][^:]*?\.lean):(?P<line>\d+):(?P<col>\d+):\s*"
    r"(?P<sev>error|warning|information|info):\s?(?P<msg>.*)$"
)
_RE_LAKE = re.compile(
    r"^(?P<sev>error|warning|information|info):\s*(?P<path>\.{0,2}[^\s:][^:]*?\.lean):"
    r"(?P<line>\d+):(?P<col>\d+):\s?(?P<msg>.*)$"
)
_STOP = ("info: ", "warning: ", "error: ", "trace: ", "✔", "✖", "⚠", "ℹ",
         "Some builds", "Build completed", "Some required builds")


def parse_text_messages(text):
    """Fallback parser for plain `lake build` / `lean` output (both layouts)."""
    msgs, cur = [], None
    for line in (text or "").splitlines():
        m = _RE_LEAN.match(line) or _RE_LAKE.match(line)
        if m:
            if cur:
                msgs.append(cur)
            sev = m.group("sev")
            cur = {
                "severity": "information" if sev == "info" else sev,
                "file": m.group("path"),
                "line": int(m.group("line")),
                "col": int(m.group("col")),
                "text": m.group("msg"),
            }
        elif cur is not None:
            if line.startswith(_STOP):
                msgs.append(cur)
                cur = None
            else:
                cur["text"] += "\n" + line
    if cur:
        msgs.append(cur)
    return msgs


def errors(msgs):
    return [m for m in msgs if m.get("severity") == "error"]


def deprecation_warnings(msgs):
    return [
        m for m in msgs
        if m.get("severity") == "warning" and "deprecat" in (m.get("text") or "").lower()
    ]


# --------------------------------------------------------------------------- #
# lakefile leanOptions extraction
# --------------------------------------------------------------------------- #

# As of mid-2026 Mathlib's lakefile.lean sets:
#   pp.unicode.fun=true, autoImplicit=false, maxSynthPendingDepth=3
#   + a set of `weak.linter.*` options.
# This is ONLY a fallback. Always prefer what we scrape from the checked-out lakefile,
# and always run the self-test in 30_make_triple.py to confirm the option set is right.
DEFAULT_LEAN_OPTS = [
    "-Dpp.unicode.fun=true",
    "-DautoImplicit=false",
    "-DmaxSynthPendingDepth=3",
]

_OPT_RE = re.compile(
    r"[\u27e8\u2329<]\s*`([A-Za-z0-9_.']+)\s*,\s*"
    r"(true|false|\.ofNat\s+(\d+)|\"([^\"]*)\")\s*[\u27e9\u232a>]"
)


def mathlib_lean_opts(repo):
    """Best-effort scrape of `leanOptions` from the checked-out lakefile.

    Returns (flags, note). Options whose name starts with `linter.` get the
    `weak.` prefix, matching Mathlib's `mathlibOnlyLinters.map (weak ++ ·)`.
    """
    lf = Path(repo) / "lakefile.lean"
    if not lf.exists():
        return list(DEFAULT_LEAN_OPTS), "lakefile.lean absent; used DEFAULT_LEAN_OPTS"
    src = lf.read_text(encoding="utf-8", errors="replace")
    flags, seen = [], set()
    for m in _OPT_RE.finditer(src):
        name = m.group(1)
        nat, string = m.group(3), m.group(4)
        if nat is not None:
            val = nat
        elif string is not None:
            val = string
        else:
            val = m.group(2)
        prefix = "weak." if name.startswith("linter.") else ""
        flag = f"-D{prefix}{name}={val}"
        if flag not in seen:
            seen.add(flag)
            flags.append(flag)
    if not flags:
        return list(DEFAULT_LEAN_OPTS), "no leanOptions matched in lakefile.lean; used DEFAULT_LEAN_OPTS"
    return flags, f"scraped {len(flags)} option(s) from lakefile.lean"


# --------------------------------------------------------------------------- #
# declaration boundaries (heuristic)
# --------------------------------------------------------------------------- #
# This is a *heuristic* line-based splitter, good enough for a one-declaration
# feasibility probe. For the real benchmark, replace it with InfoTree extraction
# (leanprover-community/repl, ntp-toolkit, LeanDojo, or Pantograph).

_DECL_RE = re.compile(
    r"^(?:private\s+|protected\s+|noncomputable\s+|nonrec\s+|partial\s+|unsafe\s+|scoped\s+|local\s+)*"
    r"(?P<kw>theorem|lemma|def|abbrev|instance|example|structure|inductive|class|alias|axiom|opaque)\b"
    r"\s*(?P<name>\u00ab[^\u00bb]+\u00bb|[A-Za-z_][^\s:(\[{\u2983]*)?"
)

_BOUNDARY_RE = re.compile(
    r"^(?:namespace|end|section|open|variable|universe|import|set_option|attribute|"
    r"macro_rules|macro|elab_rules|elab|syntax|notation|deprecated_module|"
    r"initialize|register_simp_attr|/-!|#)"
)


def _extend_start(lines, i):
    """Walk backwards from a declaration keyword over its attributes/docstring."""
    s, j = i, i - 1
    while j >= 0:
        line = lines[j]
        if line.startswith("@["):
            s, j = j, j - 1
            continue
        if line.rstrip().endswith("-/"):
            t = j
            while t >= 0 and not lines[t].startswith(("/--", "/-")):
                t -= 1
            if t >= 0 and lines[t].startswith("/--"):
                s, j = t, t - 1
                continue
            break
        break
    return s


def split_decls(src):
    """-> [{kind, name, start_line, end_line}] with 1-based inclusive lines."""
    lines = src.split("\n")
    anchors = []
    for i, line in enumerate(lines):
        m = _DECL_RE.match(line)
        if m:
            anchors.append((i, m.group("kw"), (m.group("name") or "<anon>")))
    starts = [_extend_start(lines, i) for i, _, _ in anchors]
    out = []
    for k, (i, kw, name) in enumerate(anchors):
        s = starts[k]
        e = (starts[k + 1] - 1) if k + 1 < len(anchors) else len(lines) - 1
        for j in range(i + 1, min(e, len(lines) - 1) + 1):
            if _BOUNDARY_RE.match(lines[j]):
                e = j - 1
                break
        while e > i and not lines[e].strip():
            e -= 1
        out.append(
            {
                "kind": kw,
                "name": name.strip("\u00ab\u00bb"),
                "start_line": s + 1,
                "end_line": e + 1,
            }
        )
    return out


def decl_at(decls, line):
    if line is None:
        return None
    for d in decls:
        if d["start_line"] <= line <= d["end_line"]:
            return d
    return None


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def removed_line_ranges(diff_text):
    """Line ranges in the OLD file touched by a `git diff -U0` diff."""
    out = []
    for line in (diff_text or "").splitlines():
        m = _HUNK_RE.match(line)
        if not m:
            continue
        a, b = int(m.group(1)), int(m.group(2) or 1)
        out.append((a, a + b - 1) if b > 0 else (a, a))
    return out


def module_of(path):
    assert path.endswith(".lean"), path
    return path[: -len(".lean")].replace("/", ".")
