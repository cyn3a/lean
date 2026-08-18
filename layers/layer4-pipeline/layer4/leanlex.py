"""Lightweight Lean 4 lexing and structural scanning.

Deliberately *not* a parser. It only needs to be good enough to (a) tokenise
for edit-shape analysis, (b) find the enclosing declaration and namespace
stack for a hunk, and (c) normalise text for dedup. Anything requiring real
elaboration belongs in `replay.py`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

DECL_KEYWORDS = (
    "theorem", "lemma", "def", "abbrev", "instance", "example", "structure",
    "inductive", "class", "opaque", "axiom", "noncomputable def", "irreducible_def",
)

_DECL_RE = re.compile(
    r"^(?P<mods>(?:@\[[^\]]*\]\s*|private\s+|protected\s+|noncomputable\s+|"
    r"partial\s+|unsafe\s+|nonrec\s+|scoped\s+|local\s+)*)"
    r"(?P<kw>theorem|lemma|def|abbrev|instance|example|structure|inductive|class|"
    r"opaque|axiom|irreducible_def)\b\s*(?P<name>[^\s:({\[⦃⟨]*)"
)
_NS_RE = re.compile(r"^\s*namespace\s+(?P<name>[\w.'\u00c0-\u024f\u0370-\u03ff]+)")
_END_RE = re.compile(r"^\s*end\b\s*(?P<name>[\w.'\u00c0-\u024f\u0370-\u03ff]*)")
_SECTION_RE = re.compile(r"^\s*section\b\s*(?P<name>[\w.']*)")
_IMPORT_RE = re.compile(r"^\s*import\s+(?P<mod>[\w.]+)")

#: `#adaptation_note` blocks are mathlib's in-source oracle: a human-written
#: sentence describing exactly what upstream change forced the workaround.
ADAPTATION_NOTE_RE = re.compile(
    r"#adaptation_note\s*(?:/--(?P<note>.*?)-/)?", re.DOTALL
)
PORTING_NOTE_RE = re.compile(r"--\s*Porting note[^\n]*", re.IGNORECASE)

_COMMENT_LINE_RE = re.compile(r"--.*$")
_BLOCK_COMMENT_RE = re.compile(r"/-(?!-).*?-/", re.DOTALL)
_DOC_COMMENT_RE = re.compile(r"/--.*?-/", re.DOTALL)


# ----------------------------------------------------------------- tokens

_IDENT_EXTRA = "_'!?"


def _is_ident_char(c: str) -> bool:
    # Covers Greek, subscripts, blackboard bold, primes -- all legal in Lean.
    return c.isalnum() or c in _IDENT_EXTRA


_MULTI_OPS = ("<;>", ":=", "=>", "->", "<-", "..", "|>.", "|>", "<|")


def tokenize(src: str, keep_comments: bool = False) -> list[str]:
    """Coarse token stream. Dotted names stay as one token (`Nat.succ_le`)."""
    toks: list[str] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c.isspace():
            i += 1
            continue
        if src.startswith("--", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
            if keep_comments:
                toks.append(src[i:j])
            i = j
            continue
        if src.startswith("/-", i):
            j = src.find("-/", i)
            j = n if j < 0 else j + 2
            if keep_comments:
                toks.append(src[i:j])
            i = j
            continue
        if c == '"':
            j = i + 1
            while j < n and src[j] != '"':
                j += 2 if src[j] == "\\" else 1
            toks.append(src[i:min(j + 1, n)])
            i = min(j + 1, n)
            continue
        if _is_ident_char(c):
            j = i
            while j < n:
                if _is_ident_char(src[j]):
                    j += 1
                elif src[j] == "." and j + 1 < n and _is_ident_char(src[j + 1]):
                    j += 1
                else:
                    break
            toks.append(src[i:j])
            i = j
            continue
        for op in _MULTI_OPS:
            if src.startswith(op, i):
                toks.append(op)
                i += len(op)
                break
        else:
            toks.append(c)
            i += 1
    return toks


IDENT_RE = re.compile(r"^[^\W\d]['\w.!?]*$", re.UNICODE)


def is_identifier(tok: str) -> bool:
    return bool(IDENT_RE.match(tok)) and not tok[0].isdigit()


def strip_comments(src: str) -> str:
    src = _DOC_COMMENT_RE.sub(" ", src)
    src = _BLOCK_COMMENT_RE.sub(" ", src)
    return "\n".join(_COMMENT_LINE_RE.sub("", ln) for ln in src.split("\n"))


def normalize(src: str, drop_comments: bool = True) -> str:
    """Whitespace/comment-insensitive form, for dedup and noise detection.

    Mathlib bump diffs contain a lot of pure reflow (`:= rfl` moved onto the
    previous line). Those must not become training pairs.
    """
    if drop_comments:
        src = strip_comments(src)
    return " ".join(src.split())


# ------------------------------------------------------------- structure

@dataclass
class DeclContext:
    name: str | None
    kind: str | None
    start_line: int          # 1-based, in the file scanned
    namespace: str           # dotted namespace stack at that point
    attributes: list[str]
    signature: str           # first line(s) of the declaration
    imports: list[str]


def scan_context(file_text: str, line_no: int) -> DeclContext:
    """Find the declaration enclosing 1-based `line_no`, plus its namespace."""
    lines = file_text.split("\n")
    idx = max(0, min(line_no - 1, len(lines) - 1))

    imports = [m.group("mod") for ln in lines[:400]
               if (m := _IMPORT_RE.match(ln))]

    # namespace stack: walk forward to idx, tracking namespace/section/end
    stack: list[tuple[str, str]] = []  # (kind, name)
    for ln in lines[:idx + 1]:
        if m := _NS_RE.match(ln):
            stack.append(("namespace", m.group("name")))
        elif _SECTION_RE.match(ln):
            m2 = _SECTION_RE.match(ln)
            stack.append(("section", m2.group("name") if m2 else ""))
        elif _END_RE.match(ln):
            if stack:
                stack.pop()
    ns = ".".join(nm for kind, nm in stack if kind == "namespace" and nm)

    # nearest declaration header at or above idx, at column 0
    decl_i = None
    for j in range(idx, -1, -1):
        if _DECL_RE.match(lines[j]):
            decl_i = j
            break
        # a blank line followed by a non-indented non-decl means we've left
        # the declaration entirely (e.g. hunk sits between decls)
    if decl_i is None:
        return DeclContext(None, None, line_no, ns, [], "", imports)

    m = _DECL_RE.match(lines[decl_i])
    assert m is not None
    attrs: list[str] = []
    k = decl_i
    while k > 0 and (lines[k - 1].lstrip().startswith("@[")
                     or lines[k - 1].lstrip().startswith("set_option")
                     or lines[k - 1].lstrip().startswith("#adaptation_note")):
        attrs.append(lines[k - 1].strip())
        k -= 1
    attrs.extend(re.findall(r"@\[[^\]]*\]", m.group("mods") or ""))

    sig_lines = []
    for ln in lines[decl_i:decl_i + 6]:
        sig_lines.append(ln)
        if ":=" in ln or ln.rstrip().endswith("by") or ln.strip() == "":
            break

    name = m.group("name") or None
    return DeclContext(
        name=f"{ns}.{name}" if ns and name else name,
        kind=m.group("kw"),
        start_line=decl_i + 1,
        namespace=ns,
        attributes=list(reversed(attrs)),
        signature="\n".join(sig_lines).strip(),
        imports=imports,
    )


def extract_adaptation_notes(text: str) -> list[str]:
    """Pull human-written breakage descriptions out of `#adaptation_note`."""
    notes = []
    for m in ADAPTATION_NOTE_RE.finditer(text):
        note = (m.group("note") or "").strip()
        if note:
            notes.append(" ".join(note.split()))
    return notes


_SIMP_CALL_RE = re.compile(
    r"\b(?P<tac>simp_all|simp_arith|simpa|simp|dsimp|field_simp|norm_num|aesop|"
    r"omega|gcongr|positivity|push_cast|norm_cast|rw|rewrite|erw|unfold)\b"
    r"(?P<only>\s+only)?\s*(?:\[(?P<args>[^\]]*)\])?"
)


def simp_calls(text: str) -> list[tuple[str, bool, list[str]]]:
    """(tactic, is_only, lemma_list) for each simp-family call in `text`."""
    out = []
    for m in _SIMP_CALL_RE.finditer(text):
        raw = m.group("args")
        if raw is None:
            # `simp only [a, b,` continuing past the hunk boundary
            tail = text[m.end():]
            if tail.lstrip().startswith("["):
                raw = tail.lstrip()[1:]
        args = [a.strip() for a in _split_top(raw)] if raw else []
        out.append((m.group("tac"), bool(m.group("only")), [a for a in args if a]))
    return out


def _split_top(s: str) -> list[str]:
    """Split on commas not nested in brackets/parens."""
    parts, depth, cur = [], 0, []
    for ch in s:
        if ch in "([{⟨":
            depth += 1
        elif ch in ")]}⟩":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur)); cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def set_options(text: str) -> list[tuple[str, str]]:
    return re.findall(r"set_option\s+([\w.]+)\s+(\S+)", text)


def attributes(text: str) -> list[str]:
    """Attributes in `text`, tolerating blocks truncated by a hunk boundary.

    Hunks routinely cut through `@[to_additive (attr := foo,` with no closing
    bracket; without this the attribute rules silently no-op and the edit gets
    misfiled as a plain identifier rename.
    """
    out: list[str] = []
    for m in re.finditer(r"@\[", text):
        rest = text[m.end():]
        close = rest.find("]")
        block = rest if close < 0 else rest[:close]
        for a in _split_top(block):
            a = a.strip()
            if not a:
                continue
            out.append(a)
            # `to_additive (attr := implicit_reducible, simps)` carries
            # attributes of its own; the migration lives in there.
            inner = re.search(r"attr\s*:=\s*(.*)", a, re.DOTALL)
            if inner:
                nested = inner.group(1).strip().rstrip(")")
                out.extend(x.strip() for x in _split_top(nested) if x.strip())
    return out


def in_attribute_position(text: str, token: str) -> str | None:
    """Where does `token` sit relative to an attribute block?

    Returns ``"name"`` if it *is* an attribute name (first token after `@[`,
    a comma, or `attr :=`), ``"argument"`` if it is merely referenced from
    inside one (e.g. the target of `@[deprecated foo]`), else ``None``.
    The distinction matters: renaming an attribute and renaming a declaration
    that an attribute points at are different repair classes.
    """
    for m in re.finditer(re.escape(token), text):
        before = text[:m.start()]
        if before.rfind("@[") <= before.rfind("]") and not re.search(r"attr\s*:=[^)]*$", before):
            continue
        head = re.search(r"(?:@\[|attr\s*:=|,)\s*$", before)
        return "name" if head else "argument"
    return None


def name_components(dotted: str) -> list[str]:
    return dotted.split(".")


def is_namespace_component(c: str) -> bool:
    """Lean convention: namespaces are UpperCamel, declarations are snake_case."""
    return bool(c) and c[:1].isupper()


_OPEN_RE = re.compile(r"^\s*open\s+(?P<scoped>scoped\s+)?(?P<names>.+?)(?P<in>\s+in)?\s*$")

#: A comma-separated run of lemma names with no tactic head -- i.e. the tail of
#: a multi-line `simp only [...]` that the hunk boundary cut away from its
#: head. Without this these land in the proof-rewrite catch-all.
_LEMMA_TOKEN = r"(?:←\s*|-\s*|↑\s*)?[^\W\d][\w.'!?₀-₉]*(?:\s+[\w.'!?₀-₉]+)*"
_LEMMA_LIST_RE = re.compile(
    rf"^\s*\[?\s*(?P<body>{_LEMMA_TOKEN}(?:\s*,\s*{_LEMMA_TOKEN})*)\s*,?\s*\]?"
    rf"(?:\s+at\s+[\w'⊢*\s]+)?\s*$"
)

#: Closed vocabulary of tactic names. Membership is *required* before an edit
#: is called a tactic change: without it `set_option ... in` insertions read as
#: `tactic_added` and pick up a spurious second label on thousands of hunks.
TACTIC_NAMES = frozenset("""
simp simpa simp_all simp_arith dsimp field_simp norm_num norm_cast push_cast
omega decide native_decide positivity gcongr linarith nlinarith polyrith ring
ring_nf abel abel_nf module aesop tauto trivial rfl exact exact_mod_cast refine
apply intro intros constructor cases rcases obtain rintro use exists induction
rw rewrite erw unfold delta subst convert congr ext funext filter_upwards
measurability continuity fun_prop bound have let show calc conv change
specialize replace set suffices by_cases by_contra contrapose push_neg
interval_cases first repeat all_goals any_goals try focus swap pick_goal guard
done skip assumption left right refine_lift split split_ifs nth_rewrite
nth_rw rwa simp_rw apply_fun norm_fin infer_instance exfalso absurd revert
clear rename_i case next stop sorry admit
""".split())


def open_namespaces(text: str) -> list[str] | None:
    """Namespaces opened by `text`, or None if it contains no `open` clause."""
    out, found = [], False
    for ln in text.split("\n"):
        m = _OPEN_RE.match(ln)
        if not m:
            continue
        found = True
        out.extend(n for n in m.group("names").split() if n not in {"in", "scoped"})
    return out if found else None


def lemma_list_fragment(text: str) -> list[str] | None:
    """Lemma names, if `text` is a bare lemma-list fragment (no tactic head)."""
    stripped = strip_comments(text).strip()
    if not stripped or "]" not in stripped and "," not in stripped:
        return None
    joined = " ".join(stripped.split())
    if any(t in TACTIC_NAMES for t in re.findall(r"[a-z_][\w']*", joined.split("[")[0])):
        return None
    if not _LEMMA_LIST_RE.match(joined):
        return None
    body = _LEMMA_LIST_RE.match(joined).group("body")
    return [a.strip() for a in _split_top(body) if a.strip()]


def inline_tactics(text: str) -> list[str]:
    """Tactic names appearing anywhere, not just at line start.

    Catches `... := homMk (D.map f) (by simpa using h)` -> `(by simp)`, where a
    line-head scan sees only `map` on both sides.
    """
    return [t for t in re.findall(r"[a-z_][\w']*", strip_comments(text))
            if t in TACTIC_NAMES]
