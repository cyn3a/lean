# Mathlib churn-repair feasibility probe

**One question:** does the construction actually yield `(broken, diagnostic, fix)` pairs?

**Success criterion:** one real triple, produced end-to-end, with the control checks passing.

---    

## 0. Before you start: three things that decide the outcome

### 0.1 A deprecation is usually *not* a break

Mathlib deprecates by shipping a backwards-compatible alias:

```lean
@[deprecated Foo.newName (since := "2026-03-14")]
alias Foo.oldName := Foo.newName
```

Downstream code that says `Foo.oldName` **still compiles**. You get a *warning*, not an
error. So "10 consecutive commits with known deprecations" may yield **zero hard breaks**.

That is not a reason to stop — it is the first real finding, and it reshapes the mining
target. The commits that produce genuine breaks are:

| source | what it looks like | breaks? |
|---|---|---|
| **alias removal** | deletes `@[deprecated]` aliases, deletes `Mathlib/Deprecated/*.lean` | hard error |
| **signature change** | arg order, implicit/explicit, typeclass hypothesis weakened/strengthened | hard error |
| **simp-set change** | `@[simp]` added/removed | hard error in `simp`-closed proofs, often far away |
| **generalisation** | `Monoid` → `MulOneClass`, extra hypothesis | often hard error |
| **namespace move** | decl moves namespace *without* alias | hard error |
| **plain deprecation** | alias added | warning only |

`10_pick_commits.py` classifies commits into `ALIAS_REMOVAL` / `DEPRECATION` / `OTHER`
and prints the survey counts. Look at the counts before you commit to a mining strategy.

Under Mathlib's CI settings a deprecation warning may still be CI-red. If you want to
count those as breaks, that's defensible — but say so explicitly in the paper, and check
the exact `lake build` invocation at your target commit:

```bash
grep -rn "lake build" .github/workflows/ | head -30
```

Look for `--wfail`, `-DwarningAsError=true`, `-KCI`, or a grep-for-`warning:` step.
Whatever you find is your definition of "broken". Write it down.

### 0.2 The broken state is counterfactual

Mathlib master is always green. A PR that renames `Foo.bar` also fixes every call site
**in the same commit**. So the state "old call site + new library" **never existed in
git history**. You are reconstructing it.

This is fine, and it is still meaningfully different from APRIL-style mutation — the
*edit* is a real human repair and the *break* is real API evolution, neither is
sampled from a mutation operator. But it is not "mined broken states", and a reviewer
will notice. Precise claim:

> Each instance is a real library-evolution edit made by a Mathlib maintainer,
> replayed against the post-change library to recover the diagnostic the maintainer
> was responding to.

If you want broken states that genuinely existed, use **Source B**:

- Mathlib's `nightly-testing` branch and `bump/v4.x.0` branches — the adaptation
  commits there fix breakage that really was red.
- `git grep -n "adaptation note"` in Mathlib — human-marked churn repairs.
- Downstream repos' "bump Mathlib" commits (FLT, Carleson, PrimeNumberTheoremAnd,
  LeanAPAP, ...) — here the broken state literally existed between the pin bump and
  the fix. Much smaller volume, much stronger provenance.

The strongest version of the paper probably uses Source A for scale and Source B as a
held-out, unimpeachable eval set.

### 0.3 Your APRIL comparison has a confound

APRIL is 260k tuples built by mutating verified proofs from Herald / Lean Workbook /
NuminaMath-Lean, on Lean 4.22.0-rc4. Those are **competition-style standalone theorems**.
Your benchmark is **Mathlib library code** on Lean ~4.31.

So "APRIL-trained models transfer poorly" has at least three candidate explanations:

1. synthetic corruption ≠ real churn (your thesis)
2. competition proofs ≠ library code (domain shift)
3. Lean 4.22 ≠ Lean 4.31 (toolchain shift)

You need a control that isolates (1). The cheapest one: apply APRIL's own four mutation
operators to the *same Mathlib declarations* in your benchmark, and evaluate the same
model on `{Mathlib + synthetic mutation}` vs `{Mathlib + real churn}`. If the gap
survives, (1) is doing the work. Without that control the headline claim doesn't hold.

Cost note: this control is nearly free once the harness in `30_make_triple.py` works —
it's the same checkout, same cache, different file content.

---

## 1. Machine requirements

- Linux or macOS. **60+ GB free disk.** 16 GB RAM minimum (a single Mathlib file can
  peak several GB during elaboration). Fast network.
- Python 3.9+. No third-party packages.
- `git`, `curl`, `elan`.

```bash
curl https://elan.lean-lang.org/elan-init.sh -sSf | sh -s -- -y
source $HOME/.elan/env
elan self update
elan --version   # want >= 2.0.0
```

Clone (full clone — the scripts do a lot of `git show`):

```bash
git clone https://github.com/leanprover-community/mathlib4.git ~/mathlib4
cd ~/mathlib4 && git fetch origin master
```

**Never run `git clean -xfd` in this repo.** `.lake/` is gitignored and holds the
multi-GB build output. One `git clean` costs you the whole download.

---

## 2. Step by step

### Step 1 — mine candidate commits (~2-5 min, no builds)

```bash
cd ~/lean_churn_probe
python3 10_pick_commits.py --repo ~/mathlib4 --scan 400 --mode deprecations --n 10
```

Read the survey block on stderr. It tells you, over 400 master commits:
how many are `ALIAS_REMOVAL`, how many are `DEPRECATION`, and the total `adapt_files`
count — i.e. **your benchmark's size per 400 commits of history**. Extrapolate now,
before you spend a day building. Mathlib has ~50k master commits.

For the literal "10 consecutive commits" version (measures best-case cache deltas):

```bash
python3 10_pick_commits.py --repo ~/mathlib4 --mode window --n 10 -o candidates_window.json
```

Output: `candidates.json`, oldest-first.

### Step 2 — cache probe over the 10 commits (~30-90 min)

```bash
python3 20_probe_cache.py --repo ~/mathlib4 --candidates candidates.json
```

Expect: first commit 5-20 min (full download, several GB), each subsequent commit
30 s - 3 min if they're near each other in history.

Outputs `cache_probe.csv` / `cache_probe.json`. This is the table for your notes.

Watch for:
- `success_pct < 100` or `missing_warning=true` → cache incomplete for that commit.
  This is the single most important failure mode; if it's common at your chosen
  history depth, the whole construction gets 100× more expensive.
- `toolchain` changing mid-window → each distinct toolchain is a separate ~1.5 GB
  elan download and a *complete* cache miss.

### Step 3 — the construction, by hand, on one declaration

Do this once manually before you trust the script. Pick an `ALIAS_REMOVAL` commit `C`
from `candidates.json` with `expected_pair_yield >= 1`, and one file `F` from its
`adapt_files`.

```bash
cd ~/mathlib4
C=<sha>                          # from candidates.json
F=Mathlib/Foo/Bar.lean           # from that commit's adapt_files
MOD=$(echo "${F%.lean}" | tr / .)

# 1. get to the post-change library
git checkout --detach $C
elan toolchain install $(cat lean-toolchain)
time lake exe cache get

# 2. what did the human actually change?
git show $C -- $F | head -60
git log -1 --format='%s' $C          # -> PR number

# 3. control: the file as-shipped must build clean
time lake build $MOD                 # expect: instant no-op (olean came from cache)

# 4. install the PRE-change version of just this file
git show $C^:$F > $F

# 5. the break: old usage vs new library
lake build $MOD                      # expect: nonzero rc, errors  <-- THIS IS THE DIAGNOSTIC

# 6. restore + control
git checkout -- $F
lake build $MOD                      # expect: rc 0
```

If step 5 errors → **you have a triple**:

- `broken`     = `git show $C^:$F`
- `diagnostic` = the output of step 5
- `fix`        = `git show $C:$F`  (equivalently, the diff in step 2)

If step 5 succeeds → no pair from this commit. Almost certainly it was a
deprecation-with-alias. Note it, move to an `ALIAS_REMOVAL` commit, try again.

### Step 4 — the same thing, automated and instrumented

```bash
python3 30_make_triple.py --repo ~/mathlib4 \
    --commit <sha> --file Mathlib/Foo/Bar.lean \
    --out triples/ --skip-cache-get
```

This runs the same six steps plus:

- **self-test**: `F@C` must elaborate clean under `lake env lean`. If it doesn't, the
  scraped `-D` options don't match the lakefile's `leanOptions` and every diagnostic
  you collect afterwards is wrong. This is the sharpest edge in the whole pipeline:
  `lake env lean` does **not** apply the package's `leanOptions`. Mathlib sets
  `autoImplicit=false`; without it, an unknown identifier can get silently auto-bound
  instead of erroring, and you lose the break entirely.
- **dual oracle**: `lake env lean --json` (structured positions) cross-checked against
  `lake build` (CI-faithful). If they disagree on pass/fail, the script tells you.
- **declaration attribution**: maps error positions and diff hunks to declarations, and
  flags `single_declaration_pair` when exactly one declaration is both changed and
  erroring — that's your clean, minimal unit.

Outputs `triples/<sha>__<module>.json` (the machine-readable triple) and a
`.md` you can paste into notes.

---

## 3. Decision rule

Run the probe. Then answer, in one page:

| measurement | from | what it decides |
|---|---|---|
| cache hit rate over 10 commits | `cache_probe.csv` | whether you can address history at all |
| median cache-get seconds | `cache_probe.csv` | cost per commit → max benchmark size |
| `ALIAS_REMOVAL` per 400 commits | `10_pick_commits.py` survey | pairs per unit of history |
| mean `adapt_files` per such commit | same | pairs per commit |
| `break_class` of your one triple | `30_make_triple.py` | whether deprecations break at all |
| `single_declaration_pair` | same | whether you can get declaration-level units |
| elaboration seconds per file | same | cost per pair |

**Go** if: cache hit rate is 10/10, you produced one `HARD` triple with both controls
clean, and `ALIAS_REMOVAL × adapt_files` extrapolates to ≥ a few hundred pairs over
Mathlib's history.

**Reframe** if: every deprecation is `SOFT_DEPRECATION_WARNING_ONLY`. The benchmark then
isn't "deprecation repair", it's "API evolution repair", and you mine alias removals and
signature changes instead. Still a paper; different mining code.

**Stop** if: cache misses are common at depth, or the yield extrapolates to < ~100 pairs.
Then Source B (nightly-testing / downstream bumps) is your only route and the paper is a
much smaller, more careful artifact.

---

## 4. Failure modes and what each one means

| symptom | cause | fix / meaning |
|---|---|---|
| `cache get` 0% success | commit too new (CI hasn't uploaded) or too old (pruned) | raise `--skip-recent`; if old commits fail, your usable history window is bounded — **report this number** |
| `cache get` partial success | some files never cached | `lake build` the rest (hours) or drop that commit |
| toolchain changes mid-window | Lean version bump commit | exclude (`toolchain_bump` flag); toolchain bumps are a *different* churn distribution, arguably a better one |
| `lake exe cache get` slow to start | Lake compiles `Cache/` from source on a new toolchain | ~30 s, normal, counted in the timing |
| control `F@C` has errors | wrong `-D` options, or stale `.lake` | check `lean_opts_note`; try `lake clean && lake exe cache get!` |
| oracles disagree | option scrape is wrong | fix `mathlib_lean_opts()` against the actual lakefile |
| `break_class == NONE` | deprecation alias kept it compiling | expected; see §0.1 |
| errors land in a different declaration than the diff | the fix was elsewhere in the file, or the heuristic splitter is wrong | inspect the `.md`; for scale, swap in InfoTree extraction |
| `lake build` takes 20+ min on one file | you picked a file deep in the DAG | pick shallower files; you only ever build **one module**, never its dependents |

**The single most important cost insight:** you never rebuild the reverse-dependency
cone. `lake build Mathlib.Foo.Bar` compiles exactly one module against cached oleans of
its imports. Dependents are irrelevant — the triple only needs the diagnostic from the
one file. This is what makes the construction cheap enough to scale.

At scale, also use `lake exe cache get Mathlib/Foo/Bar.lean` to fetch only that file's
transitive imports instead of all ~5k oleans.

---

## 5. Files

```
probe_lib.py         git/timing helpers, Lean message parsing, lakefile option scrape,
                     heuristic declaration splitter
10_pick_commits.py   mine + classify master commits -> candidates.json
20_probe_cache.py    checkout + cache get x10, timed -> cache_probe.{json,csv}
30_make_triple.py    the construction, one commit x one file -> triples/*.json
```

For the real benchmark, replace the heuristic declaration splitter with proper InfoTree
extraction: `leanprover-community/repl`, `ntp-toolkit`, LeanDojo, or Pantograph.
