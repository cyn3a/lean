"""Layer 4 taxonomy: edit-shape -> repair class -> predicted error class.

Two label spaces, deliberately separate:

* ``RepairLabel``  -- what the *fix* did. Derived from the diff alone, so it is
  free (no build required).
* ``ErrorClass``   -- what the *broken* side is predicted to emit. Verified
  against real diagnostics by ``replay.py``; the agreement rate is the
  calibration signal for the free labels.

Label ids are stable strings, not enum ordinals, so this can be remapped onto
an existing taxonomy from layers 1-3 via ``--taxonomy-map``.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Callable

from . import leanlex
from .diffparse import Hunk


class RepairLabel:
    # -- naming / resolution -------------------------------------------
    RENAME_DECL = "rename.decl"
    RENAME_ATTRIBUTE = "rename.attribute"
    RENAME_TACTIC = "rename.tactic"
    RENAME_OPTION = "rename.option"
    NAMESPACE_MOVE = "rename.namespace"
    DEPRECATION_ADDED = "rename.deprecation_added"
    IMPORT_DELTA = "rename.import"
    OPEN_SCOPE_DELTA = "rename.open_scope"

    # -- elaboration semantics -----------------------------------------
    DEFEQ_TRANSPARENCY = "elab.defeq_transparency"
    INSTANCE_REDUCIBILITY = "elab.instance_reducibility"
    TYPE_ASCRIPTION_ADDED = "elab.type_ascription"
    NAMED_ARG_ADDED = "elab.named_argument"
    IMPLICIT_ARG_FIX = "elab.implicit_argument"
    UNIVERSE_ANNOTATION = "elab.universe"
    COERCION_FIX = "elab.coercion"
    BINDER_SYNTAX = "elab.binder_syntax"
    STRUCTURE_INSTANCE = "elab.structure_instance"

    # -- automation drift ----------------------------------------------
    SIMP_LEMMA_ADDED = "auto.simp_lemma_added"
    SIMP_LEMMA_REMOVED = "auto.simp_lemma_removed"
    SIMP_LEMMA_REORIENTED = "auto.simp_lemma_reoriented"
    SIMP_TO_SIMP_ONLY = "auto.simp_tightened"
    TACTIC_SWAP = "auto.tactic_swap"
    TACTIC_ADDED = "auto.tactic_added"
    TACTIC_REMOVED = "auto.tactic_removed"
    PROOF_REWRITE = "auto.proof_rewrite"
    TERM_WRAPPED = "auto.term_wrapped"
    ATTRIBUTE_DELTA = "auto.attribute_delta"

    # -- resource / perf -------------------------------------------------
    HEARTBEAT_BUMP = "perf.heartbeats"
    RECURSION_DEPTH = "perf.max_rec_depth"
    SYNTH_BUDGET = "perf.synth_instance"

    # -- linters ---------------------------------------------------------
    LINTER_UNUSED = "lint.unused_variable"
    LINTER_SIMP_NF = "lint.simp_nf"
    LINTER_DISABLED = "lint.disabled"
    LINTER_OTHER = "lint.other"

    # -- meta --------------------------------------------------------------
    ADAPTATION_NOTE_ONLY = "meta.adaptation_note"
    FORMATTING = "meta.formatting"          # filtered by default
    COMMENT_ONLY = "meta.comment_only"      # filtered by default
    UNKNOWN = "meta.unknown"


class ErrorClass:
    UNKNOWN_IDENTIFIER = "unknown_identifier"
    UNKNOWN_CONSTANT = "unknown_constant"
    TYPE_MISMATCH = "type_mismatch"
    APP_TYPE_MISMATCH = "application_type_mismatch"
    SYNTH_FAILED = "failed_to_synthesize"
    INSTANCE_DEFEQ = "instance_not_defeq"
    UNSOLVED_GOALS = "unsolved_goals"
    NO_PROGRESS = "simp_made_no_progress"
    REWRITE_FAILED = "rewrite_failed"
    RFL_FAILED = "rfl_failed"
    TIMEOUT = "deterministic_timeout"
    MAX_REC = "maximum_recursion_depth"
    PARSE_ERROR = "parse_error"
    MOTIVE = "motive_not_type_correct"
    AMBIGUOUS = "ambiguous_notation"
    FIELD_NOTATION = "invalid_field_notation"
    LINTER_WARNING = "linter_warning"
    SORRY = "declaration_uses_sorry"
    NONE = "no_error"
    OTHER = "other"


#: Which diagnostics a given repair class is *expected* to be repairing.
#: replay.py scores observed-vs-expected to calibrate the free labels.
EXPECTED_ERRORS: dict[str, set[str]] = {
    RepairLabel.RENAME_DECL: {ErrorClass.UNKNOWN_IDENTIFIER, ErrorClass.UNKNOWN_CONSTANT},
    RepairLabel.RENAME_ATTRIBUTE: {ErrorClass.UNKNOWN_IDENTIFIER, ErrorClass.PARSE_ERROR},
    RepairLabel.RENAME_TACTIC: {ErrorClass.PARSE_ERROR, ErrorClass.UNKNOWN_IDENTIFIER},
    RepairLabel.RENAME_OPTION: {ErrorClass.UNKNOWN_IDENTIFIER, ErrorClass.OTHER},
    RepairLabel.NAMESPACE_MOVE: {ErrorClass.UNKNOWN_IDENTIFIER, ErrorClass.UNKNOWN_CONSTANT},
    RepairLabel.IMPORT_DELTA: {ErrorClass.UNKNOWN_IDENTIFIER, ErrorClass.UNKNOWN_CONSTANT},
    RepairLabel.OPEN_SCOPE_DELTA: {ErrorClass.AMBIGUOUS, ErrorClass.UNKNOWN_IDENTIFIER,
                                   ErrorClass.LINTER_WARNING},
    RepairLabel.DEFEQ_TRANSPARENCY: {ErrorClass.TYPE_MISMATCH, ErrorClass.APP_TYPE_MISMATCH,
                                     ErrorClass.UNSOLVED_GOALS, ErrorClass.RFL_FAILED,
                                     ErrorClass.SYNTH_FAILED},
    RepairLabel.INSTANCE_REDUCIBILITY: {ErrorClass.INSTANCE_DEFEQ, ErrorClass.SYNTH_FAILED,
                                        ErrorClass.TYPE_MISMATCH},
    RepairLabel.TYPE_ASCRIPTION_ADDED: {ErrorClass.TYPE_MISMATCH, ErrorClass.SYNTH_FAILED},
    RepairLabel.NAMED_ARG_ADDED: {ErrorClass.APP_TYPE_MISMATCH, ErrorClass.TYPE_MISMATCH},
    RepairLabel.IMPLICIT_ARG_FIX: {ErrorClass.APP_TYPE_MISMATCH, ErrorClass.SYNTH_FAILED},
    RepairLabel.UNIVERSE_ANNOTATION: {ErrorClass.TYPE_MISMATCH, ErrorClass.OTHER},
    RepairLabel.COERCION_FIX: {ErrorClass.TYPE_MISMATCH, ErrorClass.FIELD_NOTATION},
    RepairLabel.BINDER_SYNTAX: {ErrorClass.PARSE_ERROR, ErrorClass.TYPE_MISMATCH},
    RepairLabel.STRUCTURE_INSTANCE: {ErrorClass.PARSE_ERROR, ErrorClass.TYPE_MISMATCH},
    RepairLabel.SIMP_LEMMA_ADDED: {ErrorClass.UNSOLVED_GOALS},
    RepairLabel.SIMP_LEMMA_REMOVED: {ErrorClass.NO_PROGRESS, ErrorClass.UNKNOWN_CONSTANT,
                                     ErrorClass.LINTER_WARNING},
    RepairLabel.SIMP_LEMMA_REORIENTED: {ErrorClass.UNSOLVED_GOALS, ErrorClass.NO_PROGRESS},
    RepairLabel.SIMP_TO_SIMP_ONLY: {ErrorClass.UNSOLVED_GOALS, ErrorClass.TIMEOUT},
    RepairLabel.TACTIC_SWAP: {ErrorClass.UNSOLVED_GOALS, ErrorClass.REWRITE_FAILED},
    RepairLabel.TACTIC_ADDED: {ErrorClass.UNSOLVED_GOALS},
    RepairLabel.TACTIC_REMOVED: {ErrorClass.NO_PROGRESS, ErrorClass.LINTER_WARNING},
    RepairLabel.PROOF_REWRITE: {ErrorClass.UNSOLVED_GOALS, ErrorClass.REWRITE_FAILED,
                                ErrorClass.TYPE_MISMATCH},
    RepairLabel.TERM_WRAPPED: {ErrorClass.TYPE_MISMATCH, ErrorClass.APP_TYPE_MISMATCH,
                               ErrorClass.UNSOLVED_GOALS},
    RepairLabel.ATTRIBUTE_DELTA: {ErrorClass.LINTER_WARNING, ErrorClass.UNSOLVED_GOALS},
    RepairLabel.HEARTBEAT_BUMP: {ErrorClass.TIMEOUT},
    RepairLabel.RECURSION_DEPTH: {ErrorClass.MAX_REC},
    RepairLabel.SYNTH_BUDGET: {ErrorClass.TIMEOUT, ErrorClass.SYNTH_FAILED},
    RepairLabel.LINTER_UNUSED: {ErrorClass.LINTER_WARNING},
    RepairLabel.LINTER_SIMP_NF: {ErrorClass.LINTER_WARNING},
    RepairLabel.LINTER_DISABLED: {ErrorClass.LINTER_WARNING},
    RepairLabel.LINTER_OTHER: {ErrorClass.LINTER_WARNING},
    RepairLabel.DEPRECATION_ADDED: {ErrorClass.LINTER_WARNING, ErrorClass.NONE},
    # meta.* labels predict *no* error: an adaptation note, a reflow or a
    # comment change does not break anything. replay scoring `no_error` here
    # is a match, not a miss -- and any meta pair that *does* error is a
    # mislabelled hunk worth surfacing.
    RepairLabel.ADAPTATION_NOTE_ONLY: {ErrorClass.NONE},
    RepairLabel.FORMATTING: {ErrorClass.NONE},
    RepairLabel.COMMENT_ONLY: {ErrorClass.NONE},
}

#: Regex -> ErrorClass, applied to real Lean diagnostics during replay.
ERROR_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"unknown identifier"), ErrorClass.UNKNOWN_IDENTIFIER),
    (re.compile(r"unknown (constant|declaration)"), ErrorClass.UNKNOWN_CONSTANT),
    (re.compile(r"unknown (namespace|attribute|tactic|option)"), ErrorClass.UNKNOWN_IDENTIFIER),
    (re.compile(r"application type mismatch"), ErrorClass.APP_TYPE_MISMATCH),
    (re.compile(r"type mismatch"), ErrorClass.TYPE_MISMATCH),
    (re.compile(r"synthesized type class instance is not definitionally equal"),
     ErrorClass.INSTANCE_DEFEQ),
    (re.compile(r"failed to synthesize"), ErrorClass.SYNTH_FAILED),
    (re.compile(r"unsolved goals"), ErrorClass.UNSOLVED_GOALS),
    (re.compile(r"simp made no progress|dsimp made no progress"), ErrorClass.NO_PROGRESS),
    (re.compile(r"(rewrite|rw) .*(failed|did not find)|motive is not type correct"),
     ErrorClass.REWRITE_FAILED),
    (re.compile(r"motive is not type correct"), ErrorClass.MOTIVE),
    (re.compile(r"The rfl tactic failed|rfl failed"), ErrorClass.RFL_FAILED),
    (re.compile(r"\(deterministic\) timeout|maximum number of heartbeats"), ErrorClass.TIMEOUT),
    (re.compile(r"maximum recursion depth"), ErrorClass.MAX_REC),
    (re.compile(r"unexpected token|expected .*(term|command|identifier)"), ErrorClass.PARSE_ERROR),
    (re.compile(r"ambiguous, possible interpretations"), ErrorClass.AMBIGUOUS),
    (re.compile(r"invalid field notation|invalid field"), ErrorClass.FIELD_NOTATION),
    (re.compile(r"declaration uses 'sorry'"), ErrorClass.SORRY),
]

_DIAG_RE = re.compile(
    r"^(?P<file>[^\s:][^:]*):(?P<line>\d+):(?P<col>\d+):\s*"
    r"(?P<sev>error|warning|info):\s*(?P<msg>.*)$"
)


def classify_error(text: str) -> str:
    for pat, cls in ERROR_PATTERNS:
        if pat.search(text):
            return cls
    return ErrorClass.OTHER


def parse_diagnostics(stdout: str) -> list[dict]:
    """Parse `lake env lean` output into structured diagnostics."""
    diags, cur = [], None
    for line in stdout.split("\n"):
        m = _DIAG_RE.match(line)
        if m:
            if cur:
                diags.append(cur)
            cur = {
                "file": m.group("file"), "line": int(m.group("line")),
                "col": int(m.group("col")), "severity": m.group("sev"),
                "message": m.group("msg"),
            }
        elif cur is not None:
            cur["message"] += "\n" + line
    if cur:
        diags.append(cur)
    for d in diags:
        d["message"] = d["message"].strip()
        d["error_class"] = (ErrorClass.LINTER_WARNING if d["severity"] == "warning"
                            else classify_error(d["message"]))
    return diags


# --------------------------------------------------------------- classifier

@dataclass
class Verdict:
    labels: list[str] = field(default_factory=list)
    confidence: float = 0.0
    evidence: dict = field(default_factory=dict)
    noise: bool = False

    @property
    def primary(self) -> str:
        return self.labels[0] if self.labels else RepairLabel.UNKNOWN

    @property
    def expected_errors(self) -> list[str]:
        out: set[str] = set()
        for lb in self.labels:
            out |= EXPECTED_ERRORS.get(lb, set())
        return sorted(out)


Rule = Callable[[Hunk, str, str], tuple[list[str], float, dict] | None]
_RULES: list[tuple[int, str, Rule]] = []


def rule(priority: int, name: str):
    def deco(fn: Rule):
        _RULES.append((priority, name, fn))
        _RULES.sort(key=lambda t: -t[0])
        return fn
    return deco


def _added(pre: str, post: str) -> str:
    """Text present in post but not pre, line-wise."""
    pre_set = {ln.strip() for ln in pre.split("\n")}
    return "\n".join(ln for ln in post.split("\n") if ln.strip() not in pre_set)


def _removed(pre: str, post: str) -> str:
    post_set = {ln.strip() for ln in post.split("\n")}
    return "\n".join(ln for ln in pre.split("\n") if ln.strip() not in post_set)


# ---- noise -----------------------------------------------------------------

@rule(100, "formatting")
def _r_formatting(h: Hunk, pre: str, post: str):
    if leanlex.normalize(pre) == leanlex.normalize(post):
        return [RepairLabel.FORMATTING], 0.99, {"noise": True}
    return None


@rule(99, "comment_only")
def _r_comment_only(h: Hunk, pre: str, post: str):
    if (leanlex.normalize(pre, drop_comments=True)
            == leanlex.normalize(post, drop_comments=True)):
        notes = leanlex.extract_adaptation_notes(post)
        if notes:
            return [RepairLabel.ADAPTATION_NOTE_ONLY], 0.95, {"notes": notes}
        return [RepairLabel.COMMENT_ONLY], 0.9, {"noise": True}
    return None


@rule(95, "adaptation_note_added")
def _r_adaptation_note(h: Hunk, pre: str, post: str):
    """`#adaptation_note /-- ... -/` is mathlib's in-source oracle: a human
    sentence naming the upstream change that forced the workaround. Pure-add
    hunks carrying one have no code delta, so no other rule fires."""
    notes = leanlex.extract_adaptation_notes(post)
    if not notes and "#adaptation_note" not in post:
        return None
    stripped_pre = leanlex.normalize(pre)
    stripped_post = leanlex.normalize(
        post.replace("#adaptation_note", " "), drop_comments=True)
    if stripped_pre == stripped_post:
        return [RepairLabel.ADAPTATION_NOTE_ONLY], 0.95, {"notes": notes}
    return None


# ---- high-precision structural rules ---------------------------------------

@rule(90, "set_option_added")
def _r_set_option(h: Hunk, pre: str, post: str):
    added = {k: v for k, v in leanlex.set_options(_added(pre, post))}
    removed = {k: v for k, v in leanlex.set_options(_removed(pre, post))}
    if not added and not removed:
        return None
    labels, ev = [], {"set_option_added": added, "set_option_removed": removed}
    for opt in {**added, **removed}:
        if "maxHeartbeats" in opt and "synthInstance" not in opt:
            labels.append(RepairLabel.HEARTBEAT_BUMP)
        elif "synthInstance" in opt:
            labels.append(RepairLabel.SYNTH_BUDGET)
        elif "maxRecDepth" in opt:
            labels.append(RepairLabel.RECURSION_DEPTH)
        elif opt.startswith("linter."):
            labels.append(RepairLabel.LINTER_DISABLED)
        elif "isDefEq" in opt or "defeq" in opt.lower() or "Transparency" in opt:
            labels.append(RepairLabel.DEFEQ_TRANSPARENCY)
        elif opt.startswith(("backward.", "experimental.")):
            labels.append(RepairLabel.DEFEQ_TRANSPARENCY)
        else:
            labels.append(RepairLabel.LINTER_OTHER)
    notes = leanlex.extract_adaptation_notes(post)
    if notes:
        ev["notes"] = notes
    return list(dict.fromkeys(labels)), 0.93, ev


@rule(85, "attribute_delta")
def _r_attributes(h: Hunk, pre: str, post: str):
    a_pre, a_post = leanlex.attributes(pre), leanlex.attributes(post)
    if a_pre == a_post or (not a_pre and not a_post):
        return None
    added = [a for a in a_post if a not in a_pre]
    removed = [a for a in a_pre if a not in a_post]
    if not added and not removed:
        return None
    ev = {"attr_added": added, "attr_removed": removed}
    if any("reducible" in a for a in added + removed):
        labels = [RepairLabel.INSTANCE_REDUCIBILITY]
        if len(added) == 1 and len(removed) == 1:
            labels.append(RepairLabel.RENAME_ATTRIBUTE)
        return labels, 0.95, ev
    # one-for-one swap of a bare attribute name == attribute rename
    if len(added) == 1 and len(removed) == 1:
        return [RepairLabel.RENAME_ATTRIBUTE], 0.8, ev
    if any(a.startswith("deprecated") for a in added):
        return [RepairLabel.DEPRECATION_ADDED], 0.95, ev
    return [RepairLabel.ATTRIBUTE_DELTA], 0.7, ev


@rule(84, "import_delta")
def _r_imports(h: Hunk, pre: str, post: str):
    pre_i = re.findall(r"^\s*import\s+([\w.]+)", pre, re.M)
    post_i = re.findall(r"^\s*import\s+([\w.]+)", post, re.M)
    if not pre_i and not post_i:
        return None
    if set(pre_i) == set(post_i):
        return None
    return [RepairLabel.IMPORT_DELTA], 0.9, {
        "imports_added": sorted(set(post_i) - set(pre_i)),
        "imports_removed": sorted(set(pre_i) - set(post_i)),
    }


@rule(83, "open_scope_delta")
def _r_open(h: Hunk, pre: str, post: str):
    """`open Category Functor` -> `open Category`.

    A dropped or re-qualified `open` is how mathlib repairs an ambiguity
    created when a name moved namespace upstream. It is a deletion, not a
    substitution, so the rename rule never sees it.
    """
    a, b = leanlex.open_namespaces(pre), leanlex.open_namespaces(post)
    if a is None and b is None:
        return None
    a, b = a or [], b or []
    if sorted(a) == sorted(b):
        return None
    return [RepairLabel.OPEN_SCOPE_DELTA], 0.9, {
        "open_added": sorted(set(b) - set(a)),
        "open_removed": sorted(set(a) - set(b)),
    }


@rule(79, "lemma_list_fragment")
def _r_lemma_fragment(h: Hunk, pre: str, post: str):
    """A hunk cut through a multi-line `simp only [...]` shows only the tail.

    The head carrying the tactic name is outside the hunk, so `simp_calls`
    finds nothing and the edit falls to the catch-all. Recover it by
    recognising the bare lemma list, and confirm against the leading context
    that a simp-family tactic really is open above.
    """
    a = leanlex.lemma_list_fragment(pre)
    b = leanlex.lemma_list_fragment(post)
    if a is None or b is None or sorted(a) == sorted(b):
        return None
    head = "\n".join(h.ctx_before)
    if not any(t in leanlex.TACTIC_NAMES for t in leanlex.inline_tactics(head)):
        return None
    added, removed = sorted(set(b) - set(a)), sorted(set(a) - set(b))
    ev = {"simp_added": added, "simp_removed": removed, "fragment": True}
    labels = []
    flipped = [x for x in added
               if x.lstrip("←- ").strip() in {y.lstrip("←- ").strip() for y in removed}]
    if flipped:
        labels.append(RepairLabel.SIMP_LEMMA_REORIENTED)
        ev["reoriented"] = flipped
    if [x for x in added if x not in flipped]:
        labels.append(RepairLabel.SIMP_LEMMA_ADDED)
    if [x for x in removed if x not in flipped]:
        labels.append(RepairLabel.SIMP_LEMMA_REMOVED)
    return labels, 0.82, ev


@rule(62, "inline_tactic_swap")
def _r_inline_tactic(h: Hunk, pre: str, post: str):
    """Tactic change inside a term, e.g. `(by simpa using h)` -> `(by simp)`."""
    a, b = leanlex.inline_tactics(pre), leanlex.inline_tactics(post)
    if not a and not b:
        return None
    if sorted(a) == sorted(b):
        return None
    added, removed = sorted(set(b) - set(a)), sorted(set(a) - set(b))
    if not added and not removed:
        return None
    ev = {"tactics_added": added, "tactics_removed": removed, "inline": True}
    if added and removed:
        return [RepairLabel.TACTIC_SWAP], 0.65, ev
    return [RepairLabel.TACTIC_ADDED if added else RepairLabel.TACTIC_REMOVED], 0.6, ev


@rule(80, "simp_set_delta")
def _r_simp(h: Hunk, pre: str, post: str):
    pre_calls, post_calls = leanlex.simp_calls(pre), leanlex.simp_calls(post)
    if not pre_calls or not post_calls:
        return None
    pre_args = {a for _, _, args in pre_calls for a in args}
    post_args = {a for _, _, args in post_calls for a in args}
    if pre_args == post_args:
        # same lemma set: maybe `simp` -> `simp only`
        if any(not o for _, o, _ in pre_calls) and all(o for _, o, _ in post_calls):
            return [RepairLabel.SIMP_TO_SIMP_ONLY], 0.85, {}
        return None
    added, removed = sorted(post_args - pre_args), sorted(pre_args - post_args)
    ev = {"simp_added": added, "simp_removed": removed,
          "tactics": sorted({t for t, _, _ in pre_calls + post_calls})}
    labels = []
    # `foo` -> `← foo` is a reorientation, not an add+remove
    flipped = [a for a in added
               if a.lstrip("← ").strip() in {r.lstrip("← ").strip() for r in removed}]
    if flipped:
        labels.append(RepairLabel.SIMP_LEMMA_REORIENTED)
        ev["reoriented"] = flipped
    if [a for a in added if a not in flipped]:
        labels.append(RepairLabel.SIMP_LEMMA_ADDED)
    if [r for r in removed if r not in flipped]:
        labels.append(RepairLabel.SIMP_LEMMA_REMOVED)
    return labels, 0.88, ev


@rule(75, "token_rename")
def _r_rename(h: Hunk, pre: str, post: str):
    subs = token_substitutions(pre, post)
    if not subs:
        return None
    ident_subs = [(a, b) for a, b in subs
                  if leanlex.is_identifier(a) and leanlex.is_identifier(b)]
    if not ident_subs or len(ident_subs) != len(subs):
        return None
    ev = {"substitutions": ident_subs}
    a, b = ident_subs[0]
    if all(leanlex.in_attribute_position(pre, x) == "name" for x, _ in ident_subs):
        labels = [RepairLabel.RENAME_ATTRIBUTE]
        if any("reducible" in x or "reducible" in y for x, y in ident_subs):
            labels.insert(0, RepairLabel.INSTANCE_REDUCIBILITY)
        return labels, 0.9, ev
    if _is_namespace_move(a, b):
        return [RepairLabel.NAMESPACE_MOVE], 0.9, ev
    conf = 0.9 if len(ident_subs) == 1 else 0.75
    return [RepairLabel.RENAME_DECL], conf, ev


def _is_namespace_move(a: str, b: str) -> bool:
    """Relocation vs rename.

    `DirectLimit -> Module.DirectLimit` is a move. `eqRec_heq_iff_heq.mp ->
    eqRec_heq_iff.mp` is *not*: the differing component is snake_case, so it
    is a lemma rename that happens to carry a `.mp` projection. Leaning on
    Lean's UpperCamel-namespace convention separates the two cleanly.
    """
    ca, cb = leanlex.name_components(a), leanlex.name_components(b)
    if len(ca) != len(cb):
        short, long_ = (ca, cb) if len(ca) < len(cb) else (cb, ca)
        if long_[-len(short):] != short:
            return False
        return all(leanlex.is_namespace_component(c) for c in long_[:-len(short)])
    # both sides entirely UpperCamel (`Tactic` -> `Mathlib.Meta`): namespaces
    if (all(leanlex.is_namespace_component(c) for c in ca)
            and all(leanlex.is_namespace_component(c) for c in cb)):
        return True
    if len(ca) < 2 or ca[-1] != cb[-1]:
        return False
    return all(leanlex.is_namespace_component(x) and leanlex.is_namespace_component(y)
               for x, y in zip(ca[:-1], cb[:-1]) if x != y)


@rule(70, "universe")
def _r_universe(h: Hunk, pre: str, post: str):
    delta = _added(pre, post)
    if re.search(r"\.\{[^}]*\}", delta) or re.search(r"\bType\s+u\b", delta):
        if not re.search(r"\.\{[^}]*\}", pre):
            return [RepairLabel.UNIVERSE_ANNOTATION], 0.7, {}
    return None


@rule(68, "named_arg_or_ascription")
def _r_named_arg(h: Hunk, pre: str, post: str):
    added = _added(pre, post)
    named = re.findall(r"\(\s*([\w'\u03b1-\u03c9]+)\s*:=\s*", added)
    if named and not re.findall(r"\(\s*([\w'\u03b1-\u03c9]+)\s*:=\s*", pre):
        return [RepairLabel.NAMED_ARG_ADDED], 0.75, {"named_args": named}
    # `(e : T)` ascription introduced around an existing term
    if re.search(r"\([^()]+\s:\s[^()]+\)", added) and pre.strip() and post.count("(") > pre.count("("):
        return [RepairLabel.TYPE_ASCRIPTION_ADDED], 0.6, {}
    return None


@rule(66, "coercion")
def _r_coercion(h: Hunk, pre: str, post: str):
    marks = ("↑", "⇑", "(↑", "toFun", ".hom", "Subtype.val")
    d_add = sum(_added(pre, post).count(m) for m in marks)
    d_rem = sum(_removed(pre, post).count(m) for m in marks)
    if d_add != d_rem and (d_add or d_rem):
        return [RepairLabel.COERCION_FIX], 0.6, {"coercion_delta": d_add - d_rem}
    return None


@rule(64, "binder_syntax")
def _r_binder(h: Hunk, pre: str, post: str):
    pats = [
        (r"fun\s*[⟨(]", r"fun\s*\|"),
        (r"∀\s*\w+\s*[><≤≥∈]", r"∀\s*\w+,\s*\w+\s*[><≤≥∈]"),
        (r"⟨[^⟩]*⟩", r"\{\s*\w+\s*:="),
    ]
    for old_p, new_p in pats:
        if re.search(old_p, pre) and re.search(new_p, post) and not re.search(new_p, pre):
            return [RepairLabel.BINDER_SYNTAX], 0.6, {}
    if re.search(r"⟨", pre) and re.search(r"\bwhere\b|\{\s*\w+\s*:=", post):
        return [RepairLabel.STRUCTURE_INSTANCE], 0.55, {}
    return None


@rule(60, "tactic_shape")
def _r_tactic(h: Hunk, pre: str, post: str):
    def heads(text: str) -> list[str]:
        out = []
        for ln in text.split("\n"):
            for seg in re.split(r";|<;>|\bthen\b", ln):
                m = re.match(r"^\s*(?:·\s*|\.\s+)?([a-z_][\w']*)", seg)
                if m and m.group(1) in leanlex.TACTIC_NAMES:
                    out.append(m.group(1))
        return out
    hp, hq = heads(pre), heads(post)
    if hp == hq:
        return None
    added = [t for t in hq if t not in hp]
    removed = [t for t in hp if t not in hq]
    ev = {"tactics_added": added, "tactics_removed": removed}
    if added and removed:
        return [RepairLabel.TACTIC_SWAP], 0.6, ev
    if added:
        return [RepairLabel.TACTIC_ADDED], 0.55, ev
    if removed:
        return [RepairLabel.TACTIC_REMOVED], 0.55, ev
    return None


@rule(10, "fallback_proof_rewrite")
def _r_fallback(h: Hunk, pre: str, post: str):
    if not (pre.strip() and post.strip()):
        return None
    np, nq = leanlex.normalize(pre), leanlex.normalize(post)
    if not np or np == nq:
        return [RepairLabel.PROOF_REWRITE], 0.3, {}
    # `exact Quotient.sound h` -> `exact congrArg _ (Quotient.sound h)`:
    # the old term survives *inside* the new one, so the repair is an
    # elaboration wrapper rather than a rewritten proof. The wrapping is
    # usually interior, so strip any leading tactic keyword before testing.
    for probe in (np, np.split(" ", 1)[-1] if " " in np else np):
        if len(probe) >= 6 and probe in nq:
            return [RepairLabel.TERM_WRAPPED], 0.45, {
                "wrapper": nq.replace(probe, "\u2026", 1)[:120]}
    return [RepairLabel.PROOF_REWRITE], 0.3, {}


def token_substitutions(pre: str, post: str) -> list[tuple[str, str]]:
    """1-for-1 token replacements between the two sides, deduplicated.

    Returns [] if the edit is not expressible as pure token substitution
    (i.e. there are insertions or deletions), which is what makes a
    substitution safely *reversible*.
    """
    a, b = leanlex.tokenize(pre), leanlex.tokenize(post)
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    subs: list[tuple[str, str]] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if tag != "replace" or (i2 - i1) != (j2 - j1):
            return []
        for k in range(i2 - i1):
            x, y = a[i1 + k], b[j1 + k]
            if x != y:
                subs.append((x, y))
    # a genuine rename applies consistently
    mapping: dict[str, str] = {}
    for x, y in subs:
        if mapping.setdefault(x, y) != y:
            return []
    return sorted(mapping.items())


def classify(h: Hunk) -> Verdict:
    pre, post = h.old_text, h.new_text
    labels: list[str] = []
    evidence: dict = {}
    confidence = 0.0
    for _prio, name, fn in _RULES:
        try:
            res = fn(h, pre, post)
        except Exception as exc:  # a rule must never kill the pipeline
            evidence.setdefault("rule_errors", []).append(f"{name}: {exc}")
            continue
        if not res:
            continue
        lbs, conf, ev = res
        if not lbs:
            continue
        if name == "fallback_proof_rewrite" and labels:
            continue  # a real rule already fired; don't dilute it
        evidence.update(ev)
        for lb in lbs:
            if lb not in labels:
                labels.append(lb)
        confidence = max(confidence, conf)
        if lbs[0] in (RepairLabel.FORMATTING, RepairLabel.COMMENT_ONLY,
                      RepairLabel.ADAPTATION_NOTE_ONLY):
            break
    if not labels:
        labels = [RepairLabel.UNKNOWN]
    noise = bool(evidence.pop("noise", False))
    notes = leanlex.extract_adaptation_notes(h.new_text + "\n" + "\n".join(h.ctx_before))
    if notes and "notes" not in evidence:
        evidence["notes"] = notes
    return Verdict(labels=labels, confidence=confidence, evidence=evidence, noise=noise)
