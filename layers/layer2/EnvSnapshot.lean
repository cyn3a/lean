/-
  EnvSnapshot.lean — Layer 2, part 1: the reflective snapshot dumper.

  Run:  lean --run EnvSnapshot.lean out.ndjson Init Lean
        lake env lean --run EnvSnapshot.lean out.ndjson Mathlib

  Emits newline-delimited JSON (one record per line, sorted) describing
  everything about an environment that a downstream compile can trip over.
-/

import Lean                                  -- pulls in the whole Lean frontend as a *library*
open Lean                                    -- so we can write `Environment` instead of `Lean.Environment`

namespace EnvSnap                            -- our own namespace, keeps helper names from clashing

/-! ## 0. Tiny JSON helpers -/

/-- Wrap a `Nat` as JSON. `JsonNumber` is a mantissa/exponent pair; exponent 0 = an integer. -/
def jnat (n : Nat) : Json := Json.num ⟨Int.ofNat n, 0⟩

/-- Wrap a `String` as JSON. Escaping is handled by `Json.compress` later. -/
def jstr (s : String) : Json := Json.str s

/-- Wrap a `Bool` as JSON. -/
def jbool (b : Bool) : Json := Json.bool b

/-- Turn a list of strings into a JSON array. `List.map` then `List.toArray` then `Json.arr`. -/
def jstrs (xs : List String) : Json := Json.arr ((xs.map jstr).toArray)

/-- 64-bit content hash of a string, printed in decimal. `hash` comes from the `Hashable` class. -/
def h64 (s : String) : String := toString (hash s : UInt64)

/-! ## 1. Encoding Lean's metadata as short, stable strings -/

/-- Binder styles get one character each, so a whole signature becomes a short word like `"ddic"`. -/
def biChar : BinderInfo → Char
  | .default        => 'd'                   -- `(x : A)`  — explicit
  | .implicit       => 'i'                   -- `{x : A}`  — implicit, solved by unification
  | .strictImplicit => 's'                   -- `⦃x : A⦄`  — strict implicit
  | .instImplicit   => 'c'                   -- `[x : A]`  — instance implicit (typeclass)

/-- Which of the 8 kinds of constant this is. Kind changes (`def`→`theorem`) break `unfold`/`rfl`. -/
def kindStr : ConstantInfo → String
  | .axiomInfo  _ => "axiom"
  | .defnInfo   _ => "def"
  | .thmInfo    _ => "theorem"
  | .opaqueInfo _ => "opaque"
  | .quotInfo   _ => "quot"                  -- the four built-in `Quot` constants
  | .inductInfo _ => "inductive"
  | .ctorInfo   _ => "ctor"                  -- a constructor of an inductive type
  | .recInfo    _ => "rec"                   -- an auto-generated recursor

/-- `@[reducible]`/`@[irreducible]` status: affects `simp`, `unfold`, and instance search.

    VERSION-SENSITIVE. We deliberately do *not* pattern-match the constructors here.
    Lean 4.32 added `ReducibilityStatus.implicitReducible`, which turns any exhaustive
    match into a compile error — and Lean rejects "all constructors + wildcard" as a
    redundant alternative, so there is no future-proof match to write. Delegating to the
    core function `toAttrString` means new constructors are handled automatically.
    It returns bracketed strings like "[reducible]" / "[implicit_reducible]", so we strip
    the brackets to keep the snapshot field clean. `String.replace` is used rather than
    `dropWhile`/`takeWhile` because those return a `String.Slice` from Lean 4.32 on. -/
def redStr (s : ReducibilityStatus) : String :=
  (s.toAttrString.replace "[" "").replace "]" ""

/-! ## 2. Canonical printing of types

`Expr` is Lean's term representation. We print it structurally rather than
pretty-printing it, because pretty-printing depends on notation, `open`s and
`set_option pp.*` — all of which differ between toolchains, which would make
every type look "changed". Bound variables are already de Bruijn indices, so
binder *names* never appear and alpha-equivalent types print identically. -/

/-- Universe parameters are renamed freely between versions (`u` vs `u_1`), so we
    print a parameter as its *position* in the declaration's `levelParams` list. -/
def levelIdx (lps : List Name) (n : Name) : String :=
  match lps.findIdx? (· == n) with           -- `findIdx?` returns `Option Nat`: position if found
  | some i => toString i                     -- positional ⇒ stable under universe renaming
  | none   => "!" ++ n.toString              -- shouldn't happen; flag it rather than silently drop it

/-- Canonical form of a universe level. Structural recursion — Lean checks it terminates. -/
def canonLevel (lps : List Name) : Level → String
  | .zero      => "0"                                                        -- `Sort 0` = `Prop`
  | .succ l    => "s" ++ canonLevel lps l                                    -- `l + 1`
  | .max a b   => "(m " ++ canonLevel lps a ++ " " ++ canonLevel lps b ++ ")" -- `max a b`
  | .imax a b  => "(i " ++ canonLevel lps a ++ " " ++ canonLevel lps b ++ ")" -- `imax a b`
  | .param n   => "u" ++ levelIdx lps n                                      -- a bound universe var
  | .mvar _    => "?u"                                                       -- unassigned metavariable

/-- Canonical form of a term. `partial` because `Expr` recursion isn't structurally obvious to Lean. -/
partial def canonExpr (lps : List Name) : Expr → String
  | .bvar i           => "b" ++ toString i                                   -- de Bruijn index
  | .fvar _           => "?f"                                                -- free var (not in closed types)
  | .mvar _           => "?m"                                                -- metavariable
  | .sort l           => "(Sort " ++ canonLevel lps l ++ ")"
  | .const n us       => "(K " ++ n.toString ++                              -- reference to another constant
                          String.join (us.map fun u => " " ++ canonLevel lps u) ++ ")"
  | .app f a          => "(" ++ canonExpr lps f ++ " " ++ canonExpr lps a ++ ")"
  | .lam _ t b bi     => "(L" ++ (biChar bi).toString ++ " " ++              -- `fun (x : t) => b`
                          canonExpr lps t ++ " " ++ canonExpr lps b ++ ")"
  | .forallE _ t b bi => "(P" ++ (biChar bi).toString ++ " " ++              -- `(x : t) → b`
                          canonExpr lps t ++ " " ++ canonExpr lps b ++ ")"
  | .letE _ t v b _   => "(let " ++ canonExpr lps t ++ " " ++
                          canonExpr lps v ++ " " ++ canonExpr lps b ++ ")"
  | .lit (.natVal n)  => "#" ++ toString n                                   -- literal `37`
  | .lit (.strVal s)  => "$" ++ s                                            -- literal `"abc"`
  | .mdata _ e        => canonExpr lps e                                     -- metadata is noise; skip it
  | .proj s i e       => "(proj " ++ s.toString ++ " " ++ toString i ++ " " ++ canonExpr lps e ++ ")"

/-- Walk the top-level `∀`-telescope of a type, recording one `biChar` per argument.
    Result length = arity; result content = the implicitness pattern. -/
partial def binderScan : Expr → String → String
  | .forallE _ _ b bi, acc => binderScan b (acc.push (biChar bi))            -- consume one binder, recurse
  | .mdata _ e,        acc => binderScan e acc                               -- see through metadata
  | _,                 acc => acc                                            -- hit the result type: stop

/-! ## 3. Per-section dumpers

Each section is its own function. When a Lean release moves an API, exactly one
of these breaks and the rest keep working — that isolation is the whole point. -/

/-- Write one JSON value as one line. `Json.compress` = no whitespace, so lines stay diff-friendly. -/
def emit (h : IO.FS.Handle) (j : Json) : IO Unit := h.putStrLn j.compress

/-- Sort an array of names by their string form, so two snapshots line up under `diff`. -/
def sortNames (ns : Array Name) : Array Name :=
  ns.qsort (fun a b => decide (a.toString < b.toString))   -- `decide` turns the `<` Prop into a `Bool`

/-- Section: every constant, with the facts that determine whether a call site still elaborates. -/
def dumpConstants (h : IO.FS.Handle) (env : Environment) (withTypes : Bool) (keepInternal : Bool)
    : IO Nat := do
  let names ← IO.mkRef (#[] : Array Name)                  -- mutable accumulator (`IO.Ref` = a cell)
  env.constants.forM fun n _ => do                         -- `constants : SMap Name ConstantInfo`
    if keepInternal || !n.isInternalDetail then            -- drop `._eq_1`, `.match_2`, proof aux, …
      names.modify (·.push n)                              -- `modify` mutates the ref in place
  let sorted := sortNames (← names.get)
  -- Precompute the instance table once: a `PHashMap Name InstanceEntry` of every `@[instance]`.
  let insts := (Meta.instanceExtension.getState env).instanceNames
  for n in sorted do
    let some ci := env.find? n | continue                  -- `find?` returns `Option ConstantInfo`
    let lps  := ci.levelParams                             -- e.g. `[u, v]`
    let bs   := binderScan ci.type ""                      -- implicitness word, e.g. `"icdd"`
    let ct   := canonExpr lps ci.type                      -- canonical type string
    let base : List (String × Json) :=
      [ ("t",  jstr "const")                               -- record tag, so the differ can bucket lines
      , ("n",  jstr n.toString)                            -- the key we diff on
      , ("k",  jstr (kindStr ci))                          -- def / theorem / inductive / …
      , ("u",  jnat lps.length)                            -- number of universe parameters
      , ("ar", jnat bs.length)                             -- arity = length of the ∀-telescope
      , ("bi", jstr bs)                                    -- implicitness pattern
      , ("th", jstr (h64 ct))                              -- type fingerprint (cheap equality test)
      , ("red", jstr (redStr (getReducibilityStatusCore env n)))  -- @[reducible]/@[irreducible] status
      , ("cls", jbool (isClass env n))                     -- is it a type class? affects `[inst]` search
      , ("prot", jbool (isProtected env n))                -- protected ⇒ `open` doesn't expose short name
      , ("inst", jbool (insts.contains n)) ]               -- registered as an instance?
    let withStruct :=                                      -- structures: field renames break projections
      if isStructure env n then
        base ++ [("fields", jstrs ((getStructureFields env n).toList.map Name.toString))]
      else base
    let withCtors :=                                       -- inductives: ctor renames break `cases`/`match`
      match ci with
      | .inductInfo i => withStruct ++ [("ctors", jstrs (i.ctors.map Name.toString))]
      | _             => withStruct
    let full :=                                            -- full type text only when asked (it's huge)
      if withTypes then withCtors ++ [("ty", jstr ct)] else withCtors
    emit h (Json.mkObj full)
  return sorted.size

/-- Section: `@[deprecated]` annotations — the aliases that let old code keep compiling. -/
def dumpDeprecations (h : IO.FS.Handle) (env : Environment) : IO Nat := do
  let names ← IO.mkRef (#[] : Array Name)
  env.constants.forM fun n _ => names.modify (·.push n)    -- include internals: aliases can be odd names
  let mut count := 0                                       -- `mut` needs `let mut` inside a `do` block
  for n in sortNames (← names.get) do
    -- `ParametricAttribute.getParam?` reads the payload attached by `@[deprecated foo]`.
    if let some d := Linter.deprecatedAttr.getParam? env n then
      emit h (Json.mkObj
        [ ("t", jstr "deprecated")
        , ("n", jstr n.toString)                           -- the old name that still resolves
        , ("new", match d.newName? with                    -- what it forwards to, if anything
            | some m => jstr m.toString
            | none   => Json.null)
        , ("since", jstr (d.since?.getD "")) ])            -- `getD` = `Option.getD`, supply a default
      count := count + 1
  return count

/-- Section: syntax. For each parser category we record its leading tokens and its node kinds.
    A missing leading token is *exactly* the cause of "unexpected token/identifier" errors. -/
def dumpSyntax (h : IO.FS.Handle) (env : Environment) : IO Nat := do
  let st := Parser.parserExtension.getState env             -- the parser env extension's state
  let mut count := 0
  -- `categories : PersistentHashMap Name ParserCategory`; fold it into a plain list to sort it.
  let cats := st.categories.foldl (fun acc k v => acc.push (k, v)) (#[] : Array (Name × Parser.ParserCategory))
  for (catName, cat) in cats.qsort (fun a b => decide (a.1.toString < b.1.toString)) do
    -- `tables.leadingTable` is keyed by leading token; we only need the keys.
    -- `.toArray` (giving key/value pairs, keys first) is the ONE method that exists on both the
    -- pre-4.32 `RBMap` form and the 4.32+ `Std.TreeMap` form of this map, so using it here — and
    -- for the option map below — is what lets a single source compile across the whole release
    -- history bar the `loadExts` line. `p.1` is the token `Name`; we print its string form.
    let leading : Array String := cat.tables.leadingTable.toArray.map (fun p => p.1.toString)
    -- `kinds : PersistentHashMap SyntaxNodeKind Unit` — the syntax node kinds in this category.
    let kinds := cat.kinds.foldl (fun acc k _ => acc.push k.toString) (#[] : Array String)
    emit h (Json.mkObj
      [ ("t", jstr "category")
      , ("n", jstr catName.toString)                        -- e.g. `tactic`, `term`, `command`, `attr`
      , ("lead", jstrs (leading.qsort (fun a b => decide (a < b))).toList)
      , ("kinds", jstrs (kinds.qsort (fun a b => decide (a < b))).toList)
      , ("nlead", jnat leading.size)
      , ("nkinds", jnat kinds.size) ])
    count := count + 1
  -- The global token table: every string the *scanner* recognises as a token.
  let toks := st.tokens.values.qsort (fun a b => decide (a < b))
  emit h (Json.mkObj [("t", jstr "tokens"), ("all", jstrs toks.toList), ("n", jnat toks.size)])
  return count

/-- Section: tactics, as seen by the elaborator (`@[tactic]`-registered elaborators, keyed by node kind). -/
def dumpTactics (h : IO.FS.Handle) (env : Environment) : IO Nat := do
  let st := Elab.Tactic.tacticElabAttribute.ext.getState env  -- `KeyedDeclsAttribute.ExtensionState`
  -- `table : SMap Key (List entry)`. `foldM` in the identity monad `Id` = a pure fold.
  let keys : Array String := Id.run <| st.table.foldM (fun acc k _ => pure (acc.push k.toString)) #[]
  let sorted := keys.qsort (fun a b => decide (a < b))
  for k in sorted do
    emit h (Json.mkObj [("t", jstr "tacticelab"), ("n", jstr k)])
  return sorted.size

/-- Section: attributes. An unregistered attribute is an "unknown attribute" error at parse time. -/
def dumpAttributes (h : IO.FS.Handle) (env : Environment) : IO Nat := do
  let ns := (getAttributeNames env).toArray                  -- `List Name` of every registered attribute
  let sorted := ns.qsort (fun a b => decide (a.toString < b.toString))
  for n in sorted do
    let descr := match getAttributeImpl env n with           -- `Except String AttributeImpl`
      | .ok impl => impl.descr                               -- human-readable description
      | .error _ => ""
    emit h (Json.mkObj [("t", jstr "attr"), ("n", jstr n.toString), ("descr", jstr descr)])
  return sorted.size

/-- Section: options. `set_option foo` on an undeclared option is a hard error. -/
def dumpOptions (h : IO.FS.Handle) : IO Nat := do
  let decls ← getOptionDecls                                 -- `NameMap OptionDecl`, from an `IO.Ref`
  -- VERSION-SENSITIVE: `NameMap` was `RBMap` (use `.fold`) before Lean 4.32 and is
  -- `Std.TreeMap` (use `.toArray` / `.foldl`) from 4.32 on. Both are ordered maps, so
  -- `toArray` already comes out sorted; we re-sort by string form anyway for stability.
  let arr : Array (Name × OptionDecl) := decls.toArray
  let sorted := arr.qsort (fun a b => decide (a.1.toString < b.1.toString))
  for (n, d) in sorted do
    let dv := match d.defValue with                          -- `DataValue` is a small tagged union
      | .ofString s => s
      | .ofBool b   => toString b
      | .ofName m   => m.toString
      | .ofNat k    => toString k
      | .ofInt i    => toString i
      | .ofSyntax _ => "<syntax>"
    emit h (Json.mkObj
      [ ("t", jstr "option"), ("n", jstr n.toString)
      , ("default", jstr dv)                                 -- default *changes* silently alter behaviour
      , ("descr", jstr d.descr) ])
  return sorted.size

/-- Section: instances, with priority. Missing instance ⇒ "failed to synthesize"; priority ⇒ wrong pick. -/
def dumpInstances (h : IO.FS.Handle) (env : Environment) : IO Nat := do
  let st := Meta.instanceExtension.getState env
  -- `instanceNames : PHashMap Name InstanceEntry`; `foldl` is its pure fold.
  let arr : Array (Name × Nat) := st.instanceNames.foldl (fun acc n e => acc.push (n, e.priority)) #[]
  let sorted := arr.qsort (fun a b => decide (a.1.toString < b.1.toString))
  for (n, prio) in sorted do
    emit h (Json.mkObj [("t", jstr "instance"), ("n", jstr n.toString), ("prio", jnat prio)])
  return sorted.size

/-- Section: the default `simp` set. Membership changes cause "simp made no progress"/"unsolved goals". -/
def dumpSimp (h : IO.FS.Handle) (env : Environment) : IO Nat := do
  let thms := Meta.simpExtension.getState env                -- `SimpTheorems`
  -- `lemmaNames : PHashSet Origin`; `Origin.decl n` is the interesting case (a named lemma).
  let arr := thms.lemmaNames.fold (fun acc o =>
    match o with
    | .decl n _ _ => acc.push n.toString
    | .other n    => acc.push ("other:" ++ n.toString)
    | _           => acc) (#[] : Array String)
  let sorted := arr.qsort (fun a b => decide (a < b))
  for n in sorted do
    emit h (Json.mkObj [("t", jstr "simp"), ("n", jstr n)])
  -- `toUnfold` = declarations marked `@[simp]` that simp unfolds rather than rewrites with.
  let unf := (thms.toUnfold.fold (fun acc n => acc.push n.toString) (#[] : Array String)).qsort
               (fun a b => decide (a < b))
  for n in unf do
    emit h (Json.mkObj [("t", jstr "simpunfold"), ("n", jstr n)])
  return sorted.size + unf.size

/-! ## 4. Driver -/

/-- `unsafe` because `enableInitializersExecution` is unsafe: it lets imported modules run their
    `initialize` blocks, which is what populates attribute/elaborator tables. -/
unsafe def run (outPath : String) (mods : List String)
    (withTypes keepInternal allowEmpty : Bool) : IO UInt32 := do
  initSearchPath (← findSysroot)                             -- teach Lean where the `.olean` files live
  enableInitializersExecution                                -- must happen *before* any import
  let imports := mods.toArray.map fun m => ({ module := m.toName } : Import)
  -- Replay the environment from the `.olean` files.
  -- VERSION-SENSITIVE and easy to get silently wrong: from Lean 4.32 `importModules` takes
  -- `loadExts`, defaulting to `false`. Without `loadExts := true` the *kernel* data (constants)
  -- loads fine but every environment extension — instances, simp set, and more — comes back
  -- EMPTY, and the tool reports a silent zero instead of failing. Older toolchains (≤ 4.31)
  -- have no such parameter: drop it there.
  let env ← importModules imports (opts := {}) (trustLevel := 1024) (loadExts := true)
  -- `withFile` closes the handle for us. We return an exit code *out* of the callback and
  -- hand it back from `run`, so the empty-section self-check below can abort with code 3.
  IO.FS.withFile outPath .write fun h => do
    -- Header record: everything needed to interpret the rest of the file.
    emit h (Json.mkObj
      [ ("t", jstr "header")
      , ("schema", jnat 1)                                    -- bump if the record shape changes
      , ("lean", jstr Lean.versionString)                     -- the toolchain that produced this snapshot
      , ("modules", jstrs mods)                               -- what we asked to import
      , ("allModules", jstrs (env.header.moduleNames.toList.map Name.toString)) ]) -- transitive closure
    let nc ← dumpConstants h env withTypes keepInternal
    let nd ← dumpDeprecations h env
    let ns ← dumpSyntax h env
    let nt ← dumpTactics h env
    let na ← dumpAttributes h env
    let no ← dumpOptions h
    let ni ← dumpInstances h env
    let nm ← dumpSimp h env
    -- Progress goes to stderr so stdout/the file stays machine-clean.
    IO.eprintln s!"consts={nc} deprecated={nd} categories={ns} tacticElabs={nt} \
                   attrs={na} options={no} instances={ni} simp={nm}"
    -- SELF-CHECK. A section that returns 0 is almost always a silent API/config failure,
    -- not a real empty set — e.g. forgetting `loadExts := true` empties `instances` and
    -- `simp` while leaving `consts` intact. A zero here would show up in the diff as
    -- thousands of bogus `instance.absent` causes, so refuse to hand back a poisoned
    -- snapshot unless the caller insists.
    let checks := [("consts", nc), ("categories", ns), ("tacticElabs", nt),
                   ("attrs", na), ("options", no), ("instances", ni), ("simp", nm)]
    let empty := checks.filter (fun p => p.2 == 0) |>.map Prod.fst
    unless empty.isEmpty do
      IO.eprintln s!"WARNING: these sections came back empty: {empty}"
      IO.eprintln "  This is a tool failure, not an empty environment. Check that"
      IO.eprintln "  `importModules` was given `loadExts := true` and that"
      IO.eprintln "  `enableInitializersExecution` ran before any import."
      unless allowEmpty do
        IO.eprintln "  Refusing to write a snapshot that would produce a bogus diff (+allow-empty to override)."
        return (3 : UInt32)
    return (0 : UInt32)

/-- Flags may be written `--foo` or `+foo`. The `+` spelling exists because `lean --run` eats
    `--`-prefixed arguments itself unless you separate them with a bare `--`. -/
def hasFlag (args : List String) (f : String) : Bool :=
  args.contains ("--" ++ f) || args.contains ("+" ++ f)

/-- Everything that isn't a flag. -/
def positional (args : List String) : List String :=
  args.filter fun a => !(a.startsWith "--" || a.startsWith "+")

/-- Entry point. `lean --run` hands us everything after the file name. -/
unsafe def main (args : List String) : IO UInt32 := do
  let withTypes    := hasFlag args "types"                    -- also store full canonical type text
  let keepInternal := hasFlag args "internal"                 -- keep `.match_1`, `._proof_2`, …
  let allowEmpty   := hasFlag args "allow-empty"              -- suppress the empty-section self-check
  match positional args with
  | out :: mods@(_ :: _) => run out mods withTypes keepInternal allowEmpty
  | _ => do
    IO.eprintln "usage: EnvSnapshot <out.ndjson> <Module>... [+types] [+internal] [+allow-empty]"
    return 1

end EnvSnap

/-- `lean --run` looks for a top-level `main`; forward to ours. -/
unsafe def main (args : List String) : IO UInt32 := EnvSnap.main args
