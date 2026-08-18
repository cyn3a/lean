"""The lean4 test suite as versioned ground truth.

VERIFIED AGAINST leanprover/lean4 @ master, July 2026. Three things differ from the
usual assumption and all three change the parser:

  1. The extension is `.lean.out.expected`, not `.lean.expected.out`. There are also
     `.txt.out.expected` (202) and `.lean.out.ignored` (60, deliberately unchecked).

  2. Most of these are NOT LSP JSON. Layout by directory:

         tests/elab               760   plain text, 314 of which carry diagnostics;
                                        the rest are raw #eval / #print stdout
         tests/elab_fail          314   plain text, all carry diagnostics
         tests/docparse           202   plain text
         tests/server_interactive 144   JSON, but request/response *pairs* from the
                                        interactive driver (hover, plainGoal), pretty-
                                        printed across lines -- not publishDiagnostics
         tests/compile             60   plain text
         (+ ~30 in compile_bench, pkg, misc_dir, bench)

     So: sniff per file. Line-oriented JSON parsing fails on server_interactive
     because values wrap; use a raw_decode loop. And the bulk of the diagnostic
     evidence is in the *plain text* directories, which is where the parser earns
     its keep.

  3. The plain-text format now carries stable error identifiers:

         1011.lean:6:11-6:13: error(lean.unknownIdentifier): Unknown identifier `AA`

     Measured on master: 2033 messages across elab+elab_fail, of which 165 (8.1%)
     are tagged, spanning 10 distinct names. Severities: 1067 warning, 966 error,
     zero `information` (info output lands in the file unheaderd). Treat the tagged
     subset as a labelled seed, not as the whole taxonomy.

Cross-tag diffing is the cheap part: it is pure git plumbing, no toolchain, no
elaboration. `git cat-file --batch` reads a whole tag's worth of expected files in
one process, so N tags costs N streaming reads of a few MB each.
"""
from __future__ import annotations

import json
import re
import subprocess
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable, Iterator

from ..normalize import scrub, split_message
from ..schema import DriftEvent, Position, RawDiagnostic

EXPECTED_SUFFIXES = (".lean.out.expected", ".txt.out.expected")
IGNORED_SUFFIX = ".lean.out.ignored"

# FILE:L:C-L:C: sev(name): msg   |   FILE:L:C: sev: msg
HEADER = re.compile(
    r"^(?P<file>[^\s:][^:\n]*?):"
    r"(?P<l1>\d+):(?P<c1>\d+)"
    r"(?:-(?P<l2>\d+):(?P<c2>\d+))?: "
    r"(?P<sev>error|warning|information|info|trace)"
    r"(?:\((?P<name>[^)\n]*)\))?: ",
    re.M,
)

_SEV_ALIAS = {"info": "information"}


def parse_text_expected(content: str, path: str = "", toolchain: str = "") -> list[RawDiagnostic]:
    """Parse a plain-text expected-output file into diagnostics.

    Everything before the first header is dropped: in tests/elab that prefix is
    `#eval` stdout, which is program output rather than a diagnostic. Do not be
    tempted to keep it -- one file in tests/elab is a 100k-element number sequence.
    """
    matches = list(HEADER.finditer(content))
    out: list[RawDiagnostic] = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        body = content[m.end():end].rstrip("\n")
        l2 = m.group("l2")
        out.append(RawDiagnostic(
            severity=_SEV_ALIAS.get(m.group("sev"), m.group("sev")),  # type: ignore[arg-type]
            text=body,
            file=m.group("file"),
            start=Position(int(m.group("l1")), int(m.group("c1"))),
            end=Position(int(l2), int(m.group("c2"))) if l2 else None,
            error_name=m.group("name"),
            source="out.expected",
            toolchain=toolchain,
            extra={"expected_path": path, "index_in_file": i},
        ))
    return out


def parse_json_expected(content: str, path: str = "", toolchain: str = "") -> list[dict]:
    """Decode a server_interactive expected file into a list of JSON values.

    These are concatenated pretty-printed objects, so line-splitting does not work.
    Values alternate request, response, request, response -- we keep them in order
    and let the caller decide which are diagnostics.
    """
    dec = json.JSONDecoder()
    out: list[dict] = []
    i, n = 0, len(content)
    while i < n:
        while i < n and content[i] in " \t\r\n":
            i += 1
        if i >= n:
            break
        try:
            val, j = dec.raw_decode(content, i)
        except json.JSONDecodeError:
            break
        out.append(val)
        i = j
    return out


def diagnostics_from_json_values(values: list[dict], path: str = "",
                                 toolchain: str = "") -> list[RawDiagnostic]:
    """Pull anything diagnostic-shaped out of interactive-test JSON.

    Tolerant by design: the interactive driver emits several response shapes and they
    change between releases. We look for the LSP Diagnostic shape (range + message)
    wherever it appears, at any nesting depth.
    """
    found: list[RawDiagnostic] = []

    def walk(v):
        if isinstance(v, dict):
            if "message" in v and "range" in v and isinstance(v["range"], dict):
                rng = v["range"]
                st, en = rng.get("start", {}), rng.get("end", {})
                sev_num = v.get("severity")
                sev = {1: "error", 2: "warning", 3: "information", 4: "trace"}.get(
                    sev_num, "unknown")
                msg = v["message"]
                found.append(RawDiagnostic(
                    severity=sev,  # type: ignore[arg-type]
                    text=msg if isinstance(msg, str) else json.dumps(msg),
                    file=path,
                    start=Position(st.get("line", 0), st.get("character", 0)),
                    end=Position(en.get("line", 0), en.get("character", 0)),
                    error_name=v.get("code") if isinstance(v.get("code"), str) else None,
                    source="out.expected(json)",
                    toolchain=toolchain,
                    extra={"expected_path": path},
                ))
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(values)
    return found


# --- git plumbing ----------------------------------------------------------

def _git(repo: Path, *args: str, binary: bool = False):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)
    return r.stdout if binary else r.stdout.decode("utf-8", "replace")


def list_expected(repo: Path, tag: str, include_ignored: bool = False) -> list[str]:
    names = _git(repo, "ls-tree", "-r", "--name-only", tag).splitlines()
    sufs = EXPECTED_SUFFIXES + ((IGNORED_SUFFIX,) if include_ignored else ())
    return [n for n in names if n.endswith(sufs)]


def prefetch(repo: Path, tag: str, prefixes: tuple[str, ...] = ("tests/",)) -> None:
    """Materialize blobs for one tag in a single round trip.

    Only relevant on a `--filter=blob:none` partial clone. There, `git cat-file
    --batch` triggers one lazy fetch *per missing blob* -- ~1300 sequential network
    round trips per tag, which is what makes people conclude cross-version diffing
    is expensive. A path-restricted checkout batches them into one fetch, after
    which cat-file is local and instant. Harmless on a full clone.

    NOTE: this writes to the working tree and index. Run it in a clone dedicated to
    harvesting, not one you also develop in.
    """
    subprocess.run(["git", "-C", str(repo), "checkout", tag, "--", *prefixes],
                   capture_output=True, check=False)


def read_blobs(repo: Path, tag: str, paths: list[str]) -> dict[str, str]:
    """Bulk-read blobs in a single `git cat-file --batch` process.

    One process for the whole tag. Doing this per-file is ~1000x slower and is the
    only reason people think cross-version diffing is expensive.
    """
    if not paths:
        return {}
    stdin = "".join(f"{tag}:{p}\n" for p in paths).encode()
    proc = subprocess.run(["git", "-C", str(repo), "cat-file", "--batch"],
                          input=stdin, capture_output=True, check=True)
    buf, out, pos = proc.stdout, {}, 0
    for p in paths:
        nl = buf.index(b"\n", pos)
        header = buf[pos:nl].decode("utf-8", "replace").split()
        pos = nl + 1
        if len(header) < 3:          # "<oid> missing"
            continue
        size = int(header[2])
        out[p] = buf[pos:pos + size].decode("utf-8", "replace")
        pos += size + 1              # trailing newline
    return out


def harvest(repo: Path, tag: str, do_prefetch: bool = True) -> Iterator[RawDiagnostic]:
    """All diagnostics recorded in the expected-output files at one tag."""
    if do_prefetch:
        prefetch(repo, tag)
    paths = list_expected(repo, tag)
    blobs = read_blobs(repo, tag, paths)
    for p, content in blobs.items():
        head = content.lstrip()[:1]
        if head in "{[":
            vals = parse_json_expected(content, p, tag)
            yield from diagnostics_from_json_values(vals, p, tag)
        else:
            yield from parse_text_expected(content, p, tag)


# --- drift -----------------------------------------------------------------

def _key(d: RawDiagnostic) -> tuple:
    return (d.extra.get("expected_path", ""), d.extra.get("index_in_file", 0))


def _prose(d: RawDiagnostic) -> str:
    return split_message(scrub(d.text))[0]


def diff_tags(repo: Path, from_tag: str, to_tag: str,
              fuzzy_threshold: float = 0.55) -> list[DriftEvent]:
    """Message-level drift between two toolchain tags.

    Alignment is (path, index-within-file) first. That breaks whenever a test gains
    or loses a message above the one you care about, so unmatched messages get a
    second pass: within the same file, pair leftovers by prose similarity and call
    the best pairing above threshold a `moved` rather than an add+remove. Without
    this pass, a single inserted `sorry` warning reports as a dozen spurious
    rewrites and the drift signal is unusable.
    """
    a = list(harvest(repo, from_tag))
    b = list(harvest(repo, to_tag))
    A = {_key(d): d for d in a}
    B = {_key(d): d for d in b}

    events: list[DriftEvent] = []
    unmatched_a: list[RawDiagnostic] = []
    unmatched_b: list[RawDiagnostic] = []

    for k, da in A.items():
        db = B.get(k)
        if db is None:
            unmatched_a.append(da)
            continue
        pa, pb = _prose(da), _prose(db)
        if pa == pb and da.severity == db.severity and da.error_name == db.error_name:
            continue
        kind = ("retag" if da.error_name != db.error_name and pa == pb else
                "reseverity" if da.severity != db.severity and pa == pb else
                "retext")
        events.append(DriftEvent(
            path=k[0], from_tag=from_tag, to_tag=to_tag, kind=kind,
            before=pa, after=pb,
            before_name=da.error_name, after_name=db.error_name,
            similarity=SequenceMatcher(a=pa, b=pb, autojunk=False).ratio(),
        ))
    for k, db in B.items():
        if k not in A:
            unmatched_b.append(db)

    by_file_a: dict[str, list[RawDiagnostic]] = defaultdict(list)
    by_file_b: dict[str, list[RawDiagnostic]] = defaultdict(list)
    for d in unmatched_a:
        by_file_a[d.extra.get("expected_path", "")].append(d)
    for d in unmatched_b:
        by_file_b[d.extra.get("expected_path", "")].append(d)

    for path in set(by_file_a) | set(by_file_b):
        left, right = by_file_a.get(path, []), by_file_b.get(path, [])
        used_r: set[int] = set()
        for da in left:
            pa = _prose(da)
            best_i, best_r = -1, 0.0
            for i, db in enumerate(right):
                if i in used_r:
                    continue
                r = SequenceMatcher(a=pa, b=_prose(db), autojunk=False).ratio()
                if r > best_r:
                    best_i, best_r = i, r
            if best_i >= 0 and best_r >= fuzzy_threshold:
                used_r.add(best_i)
                db = right[best_i]
                events.append(DriftEvent(
                    path=path, from_tag=from_tag, to_tag=to_tag, kind="moved",
                    before=pa, after=_prose(db),
                    before_name=da.error_name, after_name=db.error_name,
                    similarity=best_r))
            else:
                events.append(DriftEvent(path=path, from_tag=from_tag, to_tag=to_tag,
                                         kind="removed", before=pa,
                                         before_name=da.error_name))
        for i, db in enumerate(right):
            if i not in used_r:
                events.append(DriftEvent(path=path, from_tag=from_tag, to_tag=to_tag,
                                         kind="added", after=_prose(db),
                                         after_name=db.error_name))
    return events


def tags_in_range(repo: Path, pattern: str = "v4.*") -> list[str]:
    out = _git(repo, "tag", "--list", pattern, "--sort=v:refname").split()
    return [t for t in out if t]
