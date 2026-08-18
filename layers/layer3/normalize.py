"""Making message text comparable across runs, machines, and toolchains.

Two halves:

  1. PP PINNING (before elaboration). Lean's rendered output depends on pretty-printer
     options and terminal width. If you don't pin these, you will measure your own
     configuration drift and call it compiler drift. `probe_options` checks each
     option against the actual toolchain rather than trusting a hardcoded list --
     option names come and go between releases, and `set_option` on an unknown
     option is itself an error that would poison every file in the run.

  2. SCRUBBING (after). Lean embeds run-specific tokens in messages -- inaccessible
     name daggers, metavariable numbers, hygienic macro scopes, absolute paths.
     These are not semantic content; left alone they shatter every template into
     singletons.
"""
from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

# Candidate options to pin. Deliberately a *candidate* list: probe_options filters it
# against the live toolchain. Add freely; unknown names cost one probe each.
CANDIDATE_PP_OPTIONS: dict[str, str] = {
    "format.width": "120",
    "maxHeartbeats": "1000000",
    "maxRecDepth": "4096",
    "pp.unicode.fun": "false",
    "pp.numericTypes": "false",
    "pp.coercions": "true",
    "pp.fullNames": "false",
    "pp.explicit": "false",
    "pp.universes": "false",
    "pp.notation": "true",
    "pp.deepTerms": "false",
    "pp.proofs": "false",
    "pp.mvars": "true",
    "pp.tagAppFns": "false",
    "pp.showLetValues": "true",
    "pp.oneline": "false",
    "trace.profiler": "false",
    "linter.all": "false",
}


def probe_options(lean_cmd: list[str], candidates: dict[str, str] | None = None,
                  timeout: int = 60) -> dict[str, str]:
    """Return the subset of `candidates` the toolchain actually accepts.

    One elaboration per candidate. Cache the result per toolchain -- it is stable
    for the life of the image.
    """
    cands = dict(candidates or CANDIDATE_PP_OPTIONS)
    accepted: dict[str, str] = {}
    with tempfile.TemporaryDirectory() as td:
        probe = Path(td) / "Probe.lean"
        for opt, val in cands.items():
            probe.write_text(f"set_option {opt} {val}\nexample : True := trivial\n")
            try:
                r = subprocess.run(lean_cmd + [str(probe)], capture_output=True,
                                   text=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                continue
            if r.returncode == 0 and "unknown option" not in (r.stdout + r.stderr):
                accepted[opt] = val
    return accepted


def preamble(options: dict[str, str]) -> str:
    """`set_option` block to prepend to a synthetic file, or pass via -D flags."""
    return "".join(f"set_option {k} {v}\n" for k, v in sorted(options.items()))


def option_flags(options: dict[str, str]) -> list[str]:
    """Same options as CLI flags, for files we must not textually modify.

    Prefer this over `preamble` on real package sources: prepending lines shifts
    every line number and silently corrupts position data.
    """
    return [f"-D{k}={v}" for k, v in sorted(options.items())]


# --- scrubbing -------------------------------------------------------------

_SUBS: list[tuple[re.Pattern[str], str]] = [
    # Inaccessible / shadowed names: `x✝`, `x✝¹`, `ty✝²`
    (re.compile(r"[A-Za-z_][A-Za-z0-9_'!?]*\u271d[\u00b9\u00b2\u00b3\u2070-\u2079]*"), "\u2039inacc\u203a"),
    # Term metavariables `?m.12345`, universe metavariables `?u.77`
    (re.compile(r"\?m\.\d+"), "?m.\u2039n\u203a"),
    (re.compile(r"\?u\.\d+"), "?u.\u2039n\u203a"),
    # Hygienic macro scopes baked into names
    (re.compile(r"\._@\.[A-Za-z0-9_.]+\._hyg\.\d+"), ".\u2039hyg\u203a"),
    (re.compile(r"\._hyg\.\d+"), ".\u2039hyg\u203a"),
    # Auto-bound universe params `u_1`, `u_23` at word boundaries
    (re.compile(r"\bu_\d+\b"), "u_\u2039n\u203a"),
    # Absolute paths -> basename. Order matters: run before the file:line rule.
    (re.compile(r"(/[\w.\-+]+)+/([\w.\-+]+\.lean)"), r"\2"),
    # Heartbeat / time / size numbers that appear in resource-limit messages
    (re.compile(r"\b\d+ heartbeats?\b"), "\u2039n\u203a heartbeats"),
    (re.compile(r"\b\d+(\.\d+)?\s?(ms|s|MB|KB|GB)\b"), "\u2039n\u203a\\2"),
    # Anonymous constructor / internal suffixes with counters
    (re.compile(r"\b(match|proof|eq|fun)_\d+\b"), "\\1_\u2039n\u203a"),
]


def scrub(text: str) -> str:
    """Remove run-specific tokens. Idempotent."""
    for pat, rep in _SUBS:
        text = pat.sub(rep, text)
    # Trailing whitespace per line, and collapse >1 blank line.
    text = "\n".join(l.rstrip() for l in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip("\n")


# --- structural split ------------------------------------------------------

_TRAILER = re.compile(r"^(Hint|Note|Explanation|Additional information)\s*:", re.M)


def split_message(text: str) -> tuple[str, str, list[str]]:
    """Split a Lean message into (prose head, indented payload, trailer labels).

    Lean messages have a consistent shape:

        <prose head, one or a few lines>
          <indented pretty-printed term / goal state>
        Hint: <trailer>

    Templating the payload token-by-token is a mistake -- it is an arbitrary term
    and will never generalize. Treat it as one opaque hole and template only the
    prose. This is the single change that makes template induction converge on this
    corpus instead of producing one template per message.
    """
    # Trailers first, so they don't leak into the payload.
    trailers: list[str] = []
    cut = len(text)
    for m in _TRAILER.finditer(text):
        trailers.append(m.group(1))
        cut = min(cut, m.start())
    body = text[:cut].rstrip()

    lines = body.split("\n")
    head_lines: list[str] = []
    payload_lines: list[str] = []
    in_payload = False
    for ln in lines:
        indented = ln.startswith("  ") or ln.startswith("\t")
        if not in_payload and indented and head_lines:
            in_payload = True
        if in_payload:
            payload_lines.append(ln)
        else:
            head_lines.append(ln)
    return ("\n".join(head_lines).strip(),
            "\n".join(payload_lines).strip(),
            trailers)


def payload_shape(payload: str) -> str:
    """Coarse fingerprint of the indented block: what kind of thing was printed."""
    if not payload:
        return "none"
    if "\u22a2" in payload:                      # turnstile: a goal state
        n = payload.count("\u22a2")
        return f"goal:{'1' if n == 1 else 'n'}"
    if re.search(r"^\s*case\s", payload, re.M):
        return "cases"
    if payload.count("\n") == 0:
        return "term:1"
    return "term:n"
