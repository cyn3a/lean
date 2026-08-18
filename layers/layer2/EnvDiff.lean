/-
  EnvDiff.lean — Layer 2, part 2: the mechanical differ.

  Run:  lean --run EnvDiff.lean base.ndjson target.ndjson causes.ndjson [--info]

  Reading direction: "I have source that compiles against BASE.
                      What breaks when I move it to TARGET?"
  So anything present in BASE and missing in TARGET is a *break*;
  anything only in TARGET is *info* (it only matters in the other direction).
-/

import Lean                                  -- we only need `Lean.Json` from this, plus `Std.HashMap`
open Lean

namespace EnvDiff

/-! ## 1. The record shapes we read back out of the snapshot -/

/-- One constant, as emitted by `EnvSnapshot.dumpConstants`.
    `deriving FromJson` writes the JSON parser for us; keys must match field names.
    Extra keys in the JSON (like `"t"`) are ignored; `Option` fields may be absent. -/
structure ConstRec where
  n      : String                            -- fully-qualified name
  k      : String                            -- def / theorem / inductive / ctor / …
  u      : Nat                               -- number of universe parameters
  ar     : Nat                               -- arity (length of the ∀-telescope)
  bi     : String                            -- implicitness word, e.g. "icdd"
  th     : String                            -- fingerprint of the canonical type
  red    : String                            -- reducibility
  cls    : Bool                              -- is a type class
  prot   : Bool                              -- is protected
  inst   : Bool                              -- is an instance
  fields : Option (Array String) := none     -- structure field names, if a structure
  ctors  : Option (Array String) := none     -- constructor names, if an inductive
  deriving FromJson, Inhabited

/-- One parser category: its leading tokens and its syntax node kinds. -/
structure CatRec where
  n     : String
  lead  : Array String
  kinds : Array String
  deriving FromJson, Inhabited

/-- Everything we loaded from one snapshot file, indexed for O(1) lookup. -/
structure Snap where
  lean   : String := ""                                    -- toolchain version string
  consts : Std.HashMap String ConstRec := {}               -- name ↦ record
  cats   : Std.HashMap String CatRec   := {}               -- category name ↦ record
  opts   : Std.HashMap String String   := {}               -- option name ↦ default value
  instPr : Std.HashMap String Nat      := {}               -- instance name ↦ priority
  deprec : Std.HashMap String String   := {}               -- deprecated name ↦ replacement ("" if none)
  attrs  : Std.HashSet String := {}                        -- registered attribute names
  tacs   : Std.HashSet String := {}                        -- tactic elaborator keys (syntax node kinds)
  toks   : Std.HashSet String := {}                        -- global scanner token table
  simps  : Std.HashSet String := {}                        -- default simp set members
  unfold : Std.HashSet String := {}                        -- `simp` unfold set members
  deriving Inhabited

/-! ## 2. Loading a snapshot file -/

/-- Read one string field out of a `Json` object, or `""` if it isn't there. -/
def str! (j : Json) (k : String) : String :=
  match j.getObjValAs? String k with | .ok s => s | .error _ => ""

/-- Read one nat field out of a `Json` object, or `0`. -/
def nat! (j : Json) (k : String) : Nat :=
  match j.getObjValAs? Nat k with | .ok n => n | .error _ => 0

/-- Read a whole `.ndjson` snapshot into a `Snap`. One pass, tag-dispatched. -/
def load (path : String) : IO Snap := do
  let lines ← IO.FS.lines path                             -- `Array String`, one JSON object each
  let mut s : Snap := {}                                   -- `let mut` = a local we may reassign
  for line in lines do
    -- Tolerate stray blank lines. `String.all` is used rather than `String.trim` because
    -- `trim` is deprecated from Lean 4.32 on and now returns a `String.Slice`.
    if line.all Char.isWhitespace then continue
    let .ok j := Json.parse line
      | throw (IO.userError s!"{path}: bad JSON line: {line.take 80}")   -- `|` = else-branch of `let`
    match str! j "t" with                                  -- the record tag decides where it goes
    | "header"     => s := { s with lean := str! j "lean" }
    | "const"      => match fromJson? (α := ConstRec) j with
                      | .ok c    => s := { s with consts := s.consts.insert c.n c }
                      | .error e => throw (IO.userError s!"const: {e}")
    | "category"   => match fromJson? (α := CatRec) j with
                      | .ok c    => s := { s with cats := s.cats.insert c.n c }
                      | .error e => throw (IO.userError s!"category: {e}")
    | "option"     => s := { s with opts   := s.opts.insert (str! j "n") (str! j "default") }
    | "instance"   => s := { s with instPr := s.instPr.insert (str! j "n") (nat! j "prio") }
    | "deprecated" => s := { s with deprec := s.deprec.insert (str! j "n") (str! j "new") }
    | "attr"       => s := { s with attrs  := s.attrs.insert (str! j "n") }
    | "tacticelab" => s := { s with tacs   := s.tacs.insert (str! j "n") }
    | "simp"       => s := { s with simps  := s.simps.insert (str! j "n") }
    | "simpunfold" => s := { s with unfold := s.unfold.insert (str! j "n") }
    | "tokens"     => match j.getObjValAs? (Array String) "all" with   -- one record holding every token
                      | .ok ts   => s := { s with toks := ts.foldl (·.insert ·) s.toks }
                      | .error _ => pure ()
    | _            => pure ()                              -- unknown tag: forward-compatible, ignore
  return s

/-! ## 3. Cause → predicted error class

This is the deterministic half of the claim: each structural cause maps to a
Layer-1 error class. Edit this table, not the diff logic, to retarget the tool. -/

/-- The closed cause set. If a diff produces a cause outside this list, the tool is incomplete —
    that is the property Layer 2 is supposed to guarantee, so keep the two in sync. -/
def allCauses : List String :=
  [ "const.absent", "const.renamed", "const.kind-changed", "const.arity-changed"
  , "const.binders-changed", "const.universes-changed", "const.type-changed"
  , "const.class-changed", "const.reducibility-changed", "const.protected-changed"
  , "const.fields-changed", "const.ctors-changed"
  , "instance.absent", "instance.priority-changed"
  , "deprecation.alias-absent", "tactic.absent"
  , "syntax.category-absent", "syntax.token-absent", "syntax.kind-absent", "token.absent"
  , "attr.absent", "option.absent", "option.default-changed"
  , "simp.lemma-absent", "simp.unfold-absent" ]

def errorClass : String → String
  | "const.absent"              => "unknown constant / unknown identifier"
  | "const.renamed"             => "unknown identifier (moved to a new name)"
  | "const.kind-changed"        => "invalid unfold / 'not a definition' / rfl failure"
  | "const.arity-changed"       => "function expected / too many arguments"
  | "const.binders-changed"     => "type mismatch / unknown named argument / implicit mismatch"
  | "const.universes-changed"   => "universe level mismatch"
  | "const.type-changed"        => "type mismatch"
  | "const.class-changed"       => "'not a class' / failed to synthesize instance"
  | "const.reducibility-changed"=> "unfold/simp/defeq behaviour change (silent or 'motive is not type correct')"
  | "const.protected-changed"   => "unknown identifier (now needs qualification)"
  | "const.fields-changed"      => "unknown field / structure instance missing field"
  | "const.ctors-changed"       => "unknown constructor / non-exhaustive match"
  | "instance.absent"           => "failed to synthesize instance"
  | "instance.priority-changed" => "wrong instance selected / diamond ambiguity"
  | "deprecation.alias-absent"  => "unknown identifier (compat alias not yet introduced)"
  | "tactic.absent"             => "unknown tactic"
  | "syntax.category-absent"    => "unknown parser category"
  | "syntax.token-absent"       => "unexpected token / unexpected identifier"
  | "syntax.kind-absent"        => "elaboration function for syntax kind not found"
  | "token.absent"              => "unexpected token"
  | "attr.absent"               => "unknown attribute"
  | "option.absent"             => "unknown option"
  | "option.default-changed"    => "silent behaviour change (no error, different result)"
  | "simp.lemma-absent"         => "simp made no progress / unsolved goals"
  | "simp.unfold-absent"        => "simp made no progress / unsolved goals"
  | _                           => "unclassified"

/-! ## 4. The diff itself -/

/-- Output handle plus two mutable counters, threaded through every section differ.
    `IO.Ref` is a mutable cell; using one means `emit` can do all the counting itself. -/
structure Ctx where
  h     : IO.FS.Handle                       -- where cause records are written
  info  : Bool                               -- also emit target-only ("info") records?
  hits  : IO.Ref Nat                         -- running total of "break" causes
  tally : IO.Ref (Std.HashMap String Nat)    -- per-cause histogram

/-- Emit one cause record and count it. `sev` is "break" (will fail) or "info" (other direction only). -/
def emit (c : Ctx) (cause name detail : String) (sev : String := "break") : IO Unit := do
  c.h.putStrLn (Json.mkObj
    [ ("cause",  Json.str cause)              -- the structural difference we found
    , ("name",   Json.str name)               -- what it happened to
    , ("detail", Json.str detail)             -- base → target, in human-readable form
    , ("class",  Json.str (errorClass cause)) -- the Layer-1 error class this predicts
    , ("sev",    Json.str sev) ]).compress
  if sev == "break" then                      -- info records don't count as breakage
    c.hits.modify (· + 1)                     -- `modify` applies a function to the cell's contents
    c.tally.modify fun m => m.insert cause ((m.getD cause 0) + 1)

/-- Run one section and report how many breaks it added, by reading the counter before and after. -/
def section' (c : Ctx) (act : IO Unit) : IO Nat := do
  let before ← c.hits.get
  act
  return (← c.hits.get) - before

/-- Sorted key list of a `HashMap`, so cause files are stable across runs. -/
def keysOf {β} (m : Std.HashMap String β) : Array String :=
  (m.toArray.map Prod.fst).qsort (fun a b => decide (a < b))

/-- Sorted element list of a `HashSet`. -/
def elemsOf (s : Std.HashSet String) : Array String :=
  s.toArray.qsort (fun a b => decide (a < b))

/-- Set difference reported as one cause per element of `base` missing from `target`. -/
def diffSet (c : Ctx) (cause : String) (base target : Std.HashSet String) : IO Unit := do
  for x in elemsOf base do
    unless target.contains x do
      emit c cause x "present in base, absent in target"
  if c.info then
    for x in elemsOf target do
      unless base.contains x do emit c (cause ++ ".reverse") x "target-only" "info"

/-- Constants: the big one. Every field difference is its own cause. -/
def diffConsts (c : Ctx) (base target : Snap) : IO Unit := do
  -- Index target-only constants by (type fingerprint, arity) so we can spot renames.
  let mut byType : Std.HashMap String (Array String) := {}
  for (n, r) in target.consts.toArray do
    unless base.consts.contains n do
      let key := r.th ++ "/" ++ toString r.ar          -- same type + same arity ⇒ rename candidate
      byType := byType.insert key ((byType.getD key #[]).push n)
  for name in keysOf base.consts do
    let some b := base.consts[name]? | continue        -- `m[k]?` is `Std.HashMap.get?`
    match target.consts[name]? with
    | none => do
      -- Absent in target. Exactly one target-only constant with the same type? Then it moved.
      let cands := byType.getD (b.th ++ "/" ++ toString b.ar) #[]
      if cands.size == 1 then
        emit c "const.renamed" name s!"{name} → {cands[0]!} (identical type fingerprint)"
      else
        emit c "const.absent" name s!"{b.k}, arity {b.ar}"
    | some t => do
      -- Present in both: compare field by field, most-specific cause first.
      if b.k != t.k then
        emit c "const.kind-changed" name s!"{b.k} → {t.k}"
      if b.ar != t.ar then
        emit c "const.arity-changed" name s!"{b.ar} → {t.ar} (binders {b.bi} → {t.bi})"
      else if b.bi != t.bi then
        emit c "const.binders-changed" name s!"{b.bi} → {t.bi}"
      if b.u != t.u then
        emit c "const.universes-changed" name s!"{b.u} → {t.u} universe params"
      -- Only report a bare type change when arity/binders/universes already agree,
      -- otherwise every arity change would be reported twice.
      if b.th != t.th && b.ar == t.ar && b.bi == t.bi && b.u == t.u then
        emit c "const.type-changed" name "type fingerprint differs"
      if b.cls != t.cls then
        emit c "const.class-changed" name s!"class={b.cls} → {t.cls}"
      if b.prot != t.prot then
        emit c "const.protected-changed" name s!"protected={b.prot} → {t.prot}"
      if b.red != t.red then
        emit c "const.reducibility-changed" name s!"{b.red} → {t.red}"
      if b.fields != t.fields then
        emit c "const.fields-changed" name s!"{b.fields.getD #[]} → {t.fields.getD #[]}"
      if b.ctors != t.ctors then
        emit c "const.ctors-changed" name s!"{b.ctors.getD #[]} → {t.ctors.getD #[]}"
  if c.info then
    for name in keysOf target.consts do
      unless base.consts.contains name do emit c "const.new" name "target-only" "info"

/-- Parser categories: a missing category, a missing leading token, a missing node kind. -/
def diffSyntax (c : Ctx) (base target : Snap) : IO Unit := do
  for cat in keysOf base.cats do
    let some b := base.cats[cat]? | continue
    match target.cats[cat]? with
    | none => emit c "syntax.category-absent" cat "category not declared in target"
    | some t => do
      let tl : Std.HashSet String := t.lead.foldl (·.insert ·) {}   -- set ⇒ O(1) membership tests
      let tk : Std.HashSet String := t.kinds.foldl (·.insert ·) {}
      for tok in b.lead.qsort (fun a b => decide (a < b)) do
        unless tl.contains tok do
          emit c "syntax.token-absent" s!"{cat}:{tok}" "leading token not registered in target"
      for k in b.kinds.qsort (fun a b => decide (a < b)) do
        unless tk.contains k do
          emit c "syntax.kind-absent" s!"{cat}:{k}" "syntax node kind not registered in target"

/-- Options: missing declarations break `set_option`; changed defaults change results silently. -/
def diffOptions (c : Ctx) (base target : Snap) : IO Unit := do
  for o in keysOf base.opts do
    match target.opts[o]? with
    | none   => emit c "option.absent" o "option not declared in target"
    | some d => if base.opts[o]! != d then emit c "option.default-changed" o s!"{base.opts[o]!} → {d}"

/-- Instances: absence breaks synthesis; priority changes silently pick a different instance. -/
def diffInstances (c : Ctx) (base target : Snap) : IO Unit := do
  for i in keysOf base.instPr do
    match target.instPr[i]? with
    | none   => emit c "instance.absent" i "not registered as an instance in target"
    | some p => if base.instPr[i]! != p then emit c "instance.priority-changed" i s!"{base.instPr[i]!} → {p}"

/-- Deprecation aliases: the compat shims that let old names keep resolving.
    Only a break if the old name is *neither* deprecated *nor* a live constant in the target. -/
def diffDeprecations (c : Ctx) (base target : Snap) : IO Unit := do
  for d in keysOf base.deprec do
    unless target.deprec.contains d || target.consts.contains d do
      emit c "deprecation.alias-absent" d s!"base forwards it to {base.deprec[d]!}"

/-! ## 5. Driver -/

def run (basePath targetPath outPath : String) (info : Bool) : IO UInt32 := do
  let base   ← load basePath                               -- what the source currently compiles against
  let target ← load targetPath                             -- what we want to move it to
  let hits  ← IO.mkRef 0
  let tally ← IO.mkRef ({} : Std.HashMap String Nat)
  IO.FS.withFile outPath .write fun h => do
    let c : Ctx := { h := h, info := info, hits := hits, tally := tally }
    emit c "meta" "diff" s!"base={base.lean} ({base.consts.size} consts) \
                           target={target.lean} ({target.consts.size} consts)" "info"
    let a ← section' c (diffConsts       c base target)
    let b ← section' c (diffSyntax       c base target)
    let d ← section' c (diffOptions      c base target)
    let e ← section' c (diffInstances    c base target)
    let f ← section' c (diffDeprecations c base target)
    let g ← section' c (diffSet c "attr.absent"        base.attrs  target.attrs)
    let i ← section' c (diffSet c "tactic.absent"      base.tacs   target.tacs)
    let j ← section' c (diffSet c "token.absent"       base.toks   target.toks)
    let k ← section' c (diffSet c "simp.lemma-absent"  base.simps  target.simps)
    let l ← section' c (diffSet c "simp.unfold-absent" base.unfold target.unfold)
    IO.eprintln s!"consts={a} syntax={b} options={d} instances={e} deprecations={f} \
                   attrs={g} tactics={i} tokens={j} simp={k}/{l}"
    -- The histogram is the point of the exercise: a finite, closed set of causes.
    let hist := (← tally.get).toArray.qsort (fun x y => decide (x.2 > y.2))  -- descending by count
    IO.eprintln "--- cause histogram (cause  count  predicted error class) ---"
    for (cause, n) in hist do
      IO.eprintln s!"{n}\t{cause}\t{errorClass cause}"
    IO.eprintln s!"TOTAL BREAKS = {← hits.get}"
  return 0

/-- Flags may be written `--foo` or `+foo`. The `+` spelling exists because `lean --run` eats
    `--`-prefixed arguments itself unless you separate them with a bare `--`. -/
def hasFlag (args : List String) (f : String) : Bool :=
  args.contains ("--" ++ f) || args.contains ("+" ++ f)

/-- Everything that isn't a flag. -/
def positional (args : List String) : List String :=
  args.filter fun a => !(a.startsWith "--" || a.startsWith "+")

def main (args : List String) : IO UInt32 := do
  if hasFlag args "table" then do                       -- print the cause → error-class table
    for c in allCauses do IO.println s!"{c}\t{errorClass c}"
    return 0
  let info := hasFlag args "info"                          -- include target-only records too
  match positional args with
  | [b, t, o] => run b t o info
  | _ => do
    IO.eprintln "usage: EnvDiff <base.ndjson> <target.ndjson> <causes.ndjson> [+info]"
    IO.eprintln "       EnvDiff +table           (print the closed cause set)"
    return 1

end EnvDiff

def main (args : List String) : IO UInt32 := EnvDiff.main args
