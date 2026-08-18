# Layer 2 — reflective environment diffing, as a per-release breaking-change chain

Three Lean programs plus a driver.

- **`EnvSnapshot.lean`** (modern) and **`EnvSnapshot_legacy.lean`** (older Lean) replay an
  environment from `.olean`s and dump a machine-readable snapshot. They are **identical
  except one line** — the modern one passes `loadExts := true` to `importModules`, which
  older Lean lacks. The dumper reads Lean internals, so it is version-locked; that is the
  only reason there are two.
- **`EnvDiff.lean`** diffs two snapshots into a cause set, each cause carrying the Layer-1
  error class it predicts. It touches **no** Lean internals, so one differ reads every
  snapshot under any toolchain.
- **`Build-Chain.ps1`** (Windows/elan) builds the adjacent-version chain.
- **`build_chain.sh`** is a Linux test mirror of the same logic.

The NDJSON snapshot is the stable contract between the two halves.

## Run

```powershell
.\Build-Chain.ps1 -VersionFile versions.txt -WithEndpoints
.\Build-Chain.ps1 -Versions 4.16.0,4.24.0,4.32.0
```

Diffs each version against its immediate neighbour only: N-1 diffs instead of N*(N-1).
Switches: `-WithEndpoints` (also diff first vs last), `-Downgrade` (also chain backwards),
`-Modules "Init Lean"`, `-Reinstall`.

Output: `chain.csv` (one row per hop, one column per cause class, plus `breaks` and
`silent` totals), `causes\<base>__<target>.ndjson` (per-hop cause sets), and
`snapshots\snap_<v>.ndjson` (cached and reusable).

The hops are only as fine-grained as `versions.txt`. For a true per-release changelog,
list every release; for a coarse migration map, list only versions you might land on.

## A chain is not a decomposition of a long jump

Measured on real snapshots (`Init Lean`, 4.16.0 / 4.24.0 / 4.32.0):

| diff | breaks |
|---|---|
| 4.16 -> 4.24 | 12,142 |
| 4.24 -> 4.32 | 21,171 |
| **hops summed** | **33,313** |
| **direct 4.16 -> 4.32** | **19,084** |

Summing hops overcounts a direct migration by 75%. Two measured sources, straight from the
snapshots:

- **3,336 constants exist only in 4.24** — added after 4.16, gone by 4.32. They inflate the
  second hop but can never affect anyone migrating 4.16 -> 4.32.
- **62 constants are removed at 4.24 and restored by 4.32** — the chain reports a break that
  the direct diff correctly does not (e.g. `Std.HashSet.Raw`, `Fin.foldr.loop`).

Nearly every cause overcounts this way; `const.renamed` is the one that *under*counts
(339 across hops vs 474 direct), because a rename whose old name disappears in one hop and
whose replacement appears in a later hop is split into an unmatched removal plus an
unmatched addition, and neither leg can pair them.

So: **per-hop rows answer "what broke in this release"; the endpoint row answers "what breaks
if I jump straight there."** Different questions. `-WithEndpoints` gets you both for one
extra diff.

## Cost

Measured: a snapshot of `Init Lean` takes ~150 s; a diff takes ~7 s. For ~33 releases:

| | snapshots | diffs | total compute |
|---|---|---|---|
| adjacent chain | 33 x 150 s ≈ 83 min | 32 x 7 s ≈ 4 min | **≈ 87 min** |
| full N x N | 33 x 150 s ≈ 83 min | 1,056 x 7 s ≈ 123 min | ≈ 206 min |

Snapshots are the reusable artifact and are cached, so re-running the chain after adding one
version costs one snapshot plus one or two diffs. Toolchain downloads (~400 MB each) usually
dominate wall-clock on a first run; snapshots are ~10-25 MB apiece.

## Why two dumpers, and why that's enough

Going 4.16 -> 4.32 the only internal-API drift that matters, after canonicalising the code:

| what changed | how it's handled |
|---|---|
| `importModules` gained `loadExts`, **defaulting to `false`** (constants load, extensions come back empty) | the single line differing between the two dumper files |
| `RBMap` -> `Std.TreeMap` for the token and option maps | both files use `.toArray`, which exists on both |
| `ReducibilityStatus` gained `implicitReducible` | neither file matches constructors; both call `toAttrString` |
| `String.trim` now returns a `String.Slice` | the differ uses `String.all Char.isWhitespace` |

That was the whole delta across 16 minor releases, so two files cover 4.16 -> latest. The
driver picks per toolchain automatically, because the exit codes are distinct where it matters:

| | older Lean | newer Lean |
|---|---|---|
| **modern** (`loadExts := true`) | compile error -> nonzero | runs -> 0 |
| **legacy** (no `loadExts`) | runs -> 0 | extensions empty -> self-check -> 3 |

It tries modern, falls back to legacy, uses whichever exits 0. Verified: 4.16 selected legacy,
4.24 and 4.32 selected modern (so the `loadExts` breakpoint is below 4.24, and the driver found
it without being told). The legacy exit-3 is the dumper's empty-extension self-check, which
refuses to write a snapshot whose extension tables silently came back empty — the
`instances=0 simp=0` failure that otherwise produces a bogus all-zero diff. Override with
`+allow-empty` only for a genuinely tiny environment.

Versions **below 4.16** are unverified and may need a third variant; the driver flags any
version where both dumpers fail rather than emitting a bad snapshot.

## Reading the output

Snapshot records are tagged by a leading `t` field (`header`, `const`, `deprecated`,
`category`, `tokens`, `tacticelab`, `attr`, `option`, `instance`, `simp`, `simpunfold`):

```json
{"t":"const","n":"Nat.add","k":"def","u":0,"ar":2,"bi":"dd",
 "th":"15898302497485402940","red":"semireducible","cls":false,"prot":true,"inst":false}
```

`ar`+`bi` are the calling convention (`bi` = one char per argument: `d/i/s/c` =
explicit/implicit/strict-implicit/instance). `th` fingerprints a canonical type rendering
(de Bruijn indices, positional universes) — compared only for equality, never parsed, so
alpha-renaming and `u`->`u_1` don't register as changes.

Cause records are directional (base = what you compile against, target = where you're moving):

```json
{"cause":"const.arity-changed","name":"Nat.add","detail":"2 -> 3 (binders dd -> ddd)",
 "class":"function expected / too many arguments","sev":"break"}
```

`EnvDiff +table` prints the closed cause set and its error-class mapping.

## Analysis order

1. **Scan the `breaks` column** for hops that spike — those are the releases that will cost you.
2. **Read the `silent` column separately, and never ignore it.** Silent causes
   (`option.default-changed`, `const.reducibility-changed`, `instance.priority-changed`,
   `simp.lemma-absent`, `simp.unfold-absent`) produce **no compiler error at all**. In the
   4.24 -> 4.32 hop, 6,167 of 21,171 breaks are silent — mostly `const.reducibility-changed`.
   Loud causes the compiler reports with a location; silent ones you find at 2 a.m.
3. **Intersect with your dependency surface.** The diff covers the whole environment; only
   causes naming constants your code references can reach you. Namespace grouping is the crude
   cut; the precise version is a `deps` field via `Expr.getUsedConstants` +
   `Environment.getModuleIdxFor?` and a reachability closure.
4. **Add `-WithEndpoints` before committing to a multi-version jump**, and trust the endpoint
   row over the sum of hops.
5. **Feed the `class` column back into Layer 1.** An observed error class with no predicting
   cause means a missing dumper section (biggest known gap: `macro_rules` bodies — a notation
   can keep its token and change meaning, which this diff cannot see).

## Files

`EnvSnapshot.lean` · `EnvSnapshot_legacy.lean` · `EnvDiff.lean` · `Build-Chain.ps1` ·
`build_chain.sh` · `versions.txt` · `lakefile.toml` (only needed to snapshot a Lake project
like Mathlib, via `lake env lean --run ...` under each toolchain).
