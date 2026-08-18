/-
  BuildChain.lean — the adjacent-version chain driver, written in Lean.

  Exists so you never touch PowerShell: Windows blocks unsigned .ps1 files by default
  (and Group Policy can block them even with -ExecutionPolicy Bypass), but you already
  have a working `lean`, and `lean --run` has no such restriction.

  Run:
    lean --run BuildChain.lean                       # reads versions.txt
    lean --run BuildChain.lean +endpoints            # also diff first vs last
    lean --run BuildChain.lean 4.16.0 4.24.0 4.32.0  # explicit version list

  Flags (use `+` — `lean --run` swallows `--` args itself):
    +endpoints   also diff first vs last (the direct jump)
    +downgrade   also chain backwards (target -> base)
    +install     run `elan toolchain install` for missing toolchains
    +force       rebuild snapshots even if cached

  Does the same three steps as Build-Chain.ps1:
    1. one snapshot per toolchain, auto-selecting modern vs legacy dumper
    2. diff each consecutive pair
    3. write chain.csv (one row per hop, one column per cause class)
-/

import Lean                                   -- only for `Lean.Json`-free string work + IO
open System                                   -- for `FilePath`

namespace BuildChain

/-! ## 0. Config -/

def snapDir   : FilePath := "snapshots"       -- cached snapshots live here
def causeDir  : FilePath := "causes"          -- one cause file per hop
def modern    : String   := "EnvSnapshot.lean"
def legacy    : String   := "EnvSnapshot_legacy.lean"
def differ    : String   := "EnvDiff.lean"
def chainCsv  : FilePath := "chain.csv"
def modules   : List String := ["Init", "Lean"]   -- what each snapshot imports

/-- The closed cause set, mirroring `allCauses` in EnvDiff.lean. One CSV column each. -/
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

/-- Causes that produce NO compiler error. Read these by hand however small the count. -/
def silentCauses : List String :=
  [ "option.default-changed", "const.reducibility-changed", "instance.priority-changed"
  , "simp.lemma-absent", "simp.unfold-absent" ]

/-! ## 1. Version handling -/

/-- Parse "4.16.0" into `(4,16,0)` so sorting is numeric, not lexicographic
    (a string sort would wrongly place "4.9.0" after "4.10.0"). -/
def parseVer (s : String) : Nat × Nat × Nat :=
  match (s.splitOn ".").map (fun p => p.toNat?) with
  | [some a, some b, some c] => (a, b, c)
  | [some a, some b]         => (a, b, 0)
  | _                        => (0, 0, 0)

/-- A UTF-8 BOM, as `IO.FS.lines` actually surfaces it: not the character U+FEFF, but the
    literal 12-character ASCII text \xef\xbb\xbf. (Verified experimentally — a character-class
    filter cannot see it, because every one of those 12 chars is printable ASCII.) -/
def bomText : String := "\\xef\\xbb\\xbf"

/-- Strip a leading UTF-8 BOM. Windows editors (Notepad, VS Code's "UTF-8 with BOM") prepend one
    to the first line of a saved file. Without this, the first line reads as "<BOM>#comment",
    which does NOT start with '#', so the comment survives the filter and is treated as a
    version — and elan is then handed nonsense. Handles both the escaped-text form above and a
    genuinely decoded U+FEFF. -/
def stripBOM (s : String) : String :=
  let cs := s.toList
  let cs := if s.startsWith bomText then cs.drop bomText.length else cs
  let cs := cs.dropWhile (fun c => c.val > 126 || c.val < 32)   -- decoded BOM / control chars
  cs.foldl (fun acc c => acc.push c) ""

/-- Accept only `MAJOR.MINOR.PATCH`, optionally with a `-rc1`-style suffix. Anything else is a
    typo, a surviving comment, or a stray blank — and feeding it to elan produces confusing
    downloads of unrelated toolchains rather than an honest error. -/
def validVersion (s : String) : Bool :=
  let core := (s.splitOn "-").headD ""
  match (core.splitOn ".").map String.toNat? with
  | [some _, some _, some _] => !core.isEmpty
  | _ => false

/-- Numeric version ordering. -/
def verLt (a b : String) : Bool :=
  let (a1,a2,a3) := parseVer a
  let (b1,b2,b3) := parseVer b
  if a1 != b1 then a1 < b1 else if a2 != b2 then a2 < b2 else a3 < b3

/-! ## 2. Running subprocesses -/

/-- Run `elan run leanprover/lean4:vV lean ARGS...`, returning the exit code.
    `elan run` sets PATH for the child, which matters because `lean` shells out to
    itself (`findSysroot`) — calling a toolchain's lean by absolute path is not enough.
    stdout/stderr are captured and written to `logPath` rather than inherited, so the
    console stays readable. -/
def runLean (ver : String) (args : List String) (logPath : FilePath) : IO UInt32 := do
  let full := #["run", s!"leanprover/lean4:v{ver}", "lean"] ++ args.toArray
  let out ← IO.Process.output { cmd := "elan", args := full }
  IO.FS.writeFile logPath (out.stdout ++ "\n--- stderr ---\n" ++ out.stderr)
  return out.exitCode

/-- Install a toolchain. Failures are non-fatal; the caller just skips that version. -/
def installToolchain (ver : String) : IO UInt32 := do
  let tc := s!"leanprover/lean4:v{ver}"
  -- Echo the exact spec: if elan then reports downloading some OTHER version, the toolchain
  -- name is the thing to look at, not the network.
  IO.println s!"    running: elan toolchain install {tc}"
  let out ← IO.Process.output { cmd := "elan", args := #["toolchain", "install", tc] }
  IO.FS.writeFile (snapDir / s!"install_{ver}.txt") (out.stdout ++ out.stderr)
  return out.exitCode

/-! ## 3. Counting causes in a diff output -/

/-- Count `"sev":"break"` records per cause. Plain string scanning — much faster than
    parsing JSON per line, and `Json.mkObj` emits keys alphabetically so `"cause"` is
    always the first field. -/
def countCauses (path : FilePath) : IO (Nat × Std.HashMap String Nat) := do
  let lines ← IO.FS.lines path
  let mut total := 0
  let mut counts : Std.HashMap String Nat := {}
  for line in lines do
    if (line.splitOn "\"sev\":\"break\"").length > 1 then       -- i.e. the line contains the marker
      -- extract the value of the leading "cause":"..." field
      match line.splitOn "\"cause\":\"" with
      | _ :: rest :: _ =>
        let c := (rest.splitOn "\"").headD ""
        counts := counts.insert c ((counts.getD c 0) + 1)
        total := total + 1
      | _ => pure ()
  return (total, counts)

/-! ## 4. Steps -/

/-- Build (or reuse) one snapshot per version. Returns the versions that succeeded. -/
def buildSnapshots (versions : List String) (doInstall force : Bool) : IO (List String) := do
  IO.FS.createDirAll snapDir
  let mut ok : List String := []
  for v in versions do
    let snap := snapDir / s!"snap_{v}.ndjson"
    if !force && (← snap.pathExists) then
      IO.println s!"[{v}] cached snapshot, reuse"
      ok := ok ++ [v]
      continue
    if doInstall then
      IO.println s!"[{v}] installing toolchain ..."
      if (← installToolchain v) != 0 then
        IO.println s!"[{v}] toolchain install failed; skipping"
        continue
    -- Try the modern dumper, fall back to legacy. Exit codes are distinct where it matters:
    --   modern on new Lean -> 0 ; modern on old Lean -> compile error (nonzero)
    --   legacy on old Lean -> 0 ; legacy on new Lean -> empty-extension self-check (3)
    let log := snapDir / s!"log_{v}.txt"
    let mut used : Option String := none
    for dumper in [modern, legacy] do
      if used.isNone then
        let code ← runLean v (["--run", dumper, snap.toString] ++ modules) log
        if code == 0 then used := some dumper
    match used with
    | some d => IO.println s!"[{v}] snapshot OK via {d}"; ok := ok ++ [v]
    | none   => IO.println s!"[{v}] BOTH dumpers failed (see {log}) -- new API breakpoint here"
  return ok

/-- One hop: diff base -> target, return the CSV row and print a summary line. -/
def diffPair (diffVer base target : String) : IO String := do
  let out := causeDir / s!"{base}__{target}.ndjson"
  let _ ← runLean diffVer
    ["--run", differ, (snapDir / s!"snap_{base}.ndjson").toString,
                      (snapDir / s!"snap_{target}.ndjson").toString, out.toString]
    (causeDir / s!"log_{base}__{target}.txt")
  let (total, counts) ← countCauses out
  let silent := silentCauses.foldl (fun acc c => acc + counts.getD c 0) 0
  IO.println s!"  {base} -> {target} : {total} breaks ({silent} silent)"
  -- CSV row: from,to,breaks,silent, then one column per cause in `allCauses` order.
  let cells := [base, target, toString total, toString silent]
               ++ allCauses.map (fun c => toString (counts.getD c 0))
  return String.intercalate "," cells

/-! ## 5. Driver -/

/-- Portable trim. `String.trim` is deprecated from Lean 4.32 on (and returns a `String.Slice`),
    while `List.asString` is deprecated in favour of `String.ofList`. Folding characters into a
    fresh string with `String.push` avoids both and compiles on every version. -/
def trimStr (s : String) : String :=
  let cs := s.toList
  let cs := cs.dropWhile Char.isWhitespace
  let cs := (cs.reverse.dropWhile Char.isWhitespace).reverse
  cs.foldl (fun acc c => acc.push c) ""

def hasFlag (args : List String) (f : String) : Bool :=
  args.contains ("--" ++ f) || args.contains ("+" ++ f)

def main (args : List String) : IO UInt32 := do
  let doInstall := hasFlag args "install"
  let force     := hasFlag args "force"
  let endpoints := hasFlag args "endpoints"
  let downgrade := hasFlag args "downgrade"
  let positional := args.filter fun a => !(a.startsWith "--" || a.startsWith "+")

  -- Version list: explicit args win, else read versions.txt (ignoring # comments).
  let versionsRaw ← if positional.isEmpty then do
      let f : FilePath := "versions.txt"
      if !(← f.pathExists) then
        IO.eprintln "no versions given and versions.txt not found (one version per line)"
        return 1
      let ls ← IO.FS.lines f
      -- strip BOM from the first line, trim, drop blanks and # comments
      let cleaned := (ls.toList.map (fun l => trimStr (stripBOM l))).filter
                       fun l => !l.isEmpty && !(l.startsWith "#")
      pure cleaned
    else pure positional
  -- Reject anything that isn't a real version BEFORE invoking elan.
  let bad := versionsRaw.filter (fun v => !validVersion v)
  unless bad.isEmpty do
    IO.eprintln s!"unparseable version entries: {bad}"
    IO.eprintln "expected lines like `4.16.0`. Check versions.txt for stray text or an editor-added BOM."
    return 1
  -- Sort numerically so "adjacent" really means adjacent.
  let versions := (versionsRaw.toArray.insertionSort verLt).toList

  if versions.length < 2 then
    IO.eprintln s!"need at least two versions; got {versions.length}"
    return 1
  IO.println s!"versions: {versions}"

  IO.FS.createDirAll causeDir
  let ok ← buildSnapshots versions doInstall force
  if ok.length < 2 then
    IO.eprintln s!"need at least two good snapshots to diff; got {ok.length}"
    return 1

  -- The differ touches no Lean internals, so any installed toolchain can run it;
  -- use the newest good one.
  let diffVer := ok.getLast!
  IO.println s!"diffing under {diffVer}"

  let mut rows : List String := []
  IO.println "=== chain (adjacent hops) ==="
  for i in [0 : ok.length - 1] do
    let b := ok[i]!
    let t := ok[i+1]!
    rows := rows ++ [← diffPair diffVer b t]
    if downgrade then
      rows := rows ++ [← diffPair diffVer t b]

  if endpoints && ok.length > 2 then
    IO.println "=== endpoint (first vs last: the direct jump) ==="
    rows := rows ++ [← diffPair diffVer ok.head! ok.getLast!]

  let header := String.intercalate "," (["from","to","breaks","silent"] ++ allCauses)
  IO.FS.writeFile chainCsv (String.intercalate "\n" (header :: rows) ++ "\n")
  IO.println ""
  IO.println s!"wrote {chainCsv} (one row per hop, one column per cause class)"
  IO.println s!"      {causeDir}/*.ndjson (per-hop cause sets), {snapDir}/snap_*.ndjson (reusable)"
  if endpoints && ok.length > 2 then
    IO.println ""
    IO.println "NOTE: summing hops OVERCOUNTS a direct migration -- it also counts churn"
    IO.println "(changed-and-changed-back, plus constants that only ever existed in an"
    IO.println "intermediate version). Trust the endpoint row for a multi-version jump."
  return 0

end BuildChain

def main (args : List String) : IO UInt32 := BuildChain.main args
