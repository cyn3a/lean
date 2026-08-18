# Layer 3 — empirical realization and frequency weights

Turns three Lean 4 corpora into fingerprinted **realizations** (the surface forms a
diagnostic actually takes) and **frequency weights** (how often each is hit), with a
declared seam to Layers 1–2.

---

## Corpus facts, verified against `leanprover/lean4` @ master

Three details in the brief differ from what is in the tree, and each one changes the
implementation, so they are worth stating before anything else.

**1. The extension is `.lean.out.expected`, not `.lean.expected.out`.** There are
1309 of them under `tests/`, plus 202 `.txt.out.expected` and 60 `.lean.out.ignored`
(deliberately unchecked — exclude these or you will measure known-broken output).

**2. Most are not JSON.** Layout:

| directory | files | format |
|---|---|---|
| `tests/elab` | 760 | plain text; only **314** contain diagnostics, the rest are raw `#eval`/`#print` stdout |
| `tests/elab_fail` | 314 | plain text, all diagnostic-bearing |
| `tests/docparse` | 202 | plain text |
| `tests/server_interactive` | 144 | JSON — but request/response *pairs* from the interactive driver (hover, `plainGoal`), pretty-printed across lines |
| `tests/compile` (+ `compile_bench`, `pkg`, `misc_dir`, `bench`) | ~93 | plain text |

So the bulk of the diagnostic evidence lives in the *text* directories, and the JSON
that does exist is not `publishDiagnostics` — it is driver transcript. Two
consequences baked into `corpora/expected_out.py`: sniff format per file, and decode
the JSON with a `raw_decode` loop rather than line-splitting, because values wrap.
Also: skip everything before the first diagnostic header in `tests/elab`, since one
file there is a ~100k-element number sequence.

**3. The text format now carries stable error identifiers.**

```
1011.lean:6:11-6:13: error(lean.unknownIdentifier): Unknown identifier `AA`
```

Measured over `tests/elab` + `tests/elab_fail`: **2033 messages, 165 tagged (8.1%),
10 distinct names**; 1067 warning / 966 error / 0 information. Full inventory in
`data/observed_error_names.json`.

This is the most consequential finding for the design. The tagged subset is too small
to be the taxonomy, but it is exact, and it is the only **labelled validation set**
available for template induction. `TemplateIndex.purity_against_error_names` drops the
tags, induces blind, and checks whether messages sharing a name landed on one
template. On `tests/elab_fail` today: 5 of 10 names split across templates, 2
templates mix names. That is a real accuracy number rather than an impression, and it
is the number to watch as tagging coverage grows.

**4. `collectDiagnostics` is the interactive test driver's directive spelling, not an
LSP method.** At the wire level you `didOpen`, wait for processing to finish, and
collect `textDocument/publishDiagnostics`. `drivers/harness.py` requests
`$/lean/waitForDiagnostics` and falls back to a quiescence timer if the server
returns an error, because custom request names have moved between releases. Which
path was used is recorded in `extra.settled_via` so you can audit it.

---

## The design decision that matters: corpus roles

**The test suite is not a frequency sample.** It is adversarially enriched — it exists
to cover rare paths, so its message distribution is close to the *inverse* of what
users hit. Counting it into frequency weights would rank `lean.inductionWithNoAlts`
alongside type mismatch. So the corpora are assigned disjoint roles:

| corpus | evidence for | evidence for |
|---|---|---|
| test suite (`*.out.expected`) | ✅ realization catalogue, message drift | ❌ frequency |
| Reservoir packages, mathlib | ✅ frequency | ⚠️ partial realization (rare paths never fire) |

`schema.FREQUENCY_STRATA` enforces this: `StratumPrior` raises if you assign non-zero
weight to a test-suite stratum.

There is a second sampling caveat with no clean fix. Git history contains *committed*
states, which are by construction error-free at their pinned toolchain. Errors
recovered from mathlib history therefore come from **toolchain mismatch** —
deprecations, renames, breaking changes — not from the developer-typo distribution.
That is a real and useful distribution (it is exactly what a migration tool needs),
but it is not "errors people encounter while writing proofs." Capturing that would
need editor telemetry. The strata are named `*_head` vs `*_bump` so this never gets
conflated, and the two get separate declared priors.

Because priors cannot be estimated from the corpora themselves, they are
analyst-declared, printed in the output, and shipped with a `sensitivity` function.
A weight that changes rank under a plausible reweighting is not a finding.

---

## The other two things that will bite

**Cascade.** One root error produces many downstream diagnostics — a failed import
poisons the file, a tactic failure yields the error plus unsolved-goals plus
`declaration uses sorry`. Raw counts measure error *recovery*, not error *frequency*.
`weights.collapse_cascades` marks descendants by range containment and
position-ordered heuristics; both counts are reported, because "what does a user see"
wants raw and "what went wrong" wants roots.

**Clustering.** Diagnostics within a file are not independent draws, so the bootstrap
resamples *files*, never diagnostics. This is not pedantry — in the smoke test, one
realization with n=28 occurrences spread over just 3 files gets a 95% interval of
[0.002, 0.070], while one with n=23 over 23 files gets [0.016, 0.038]. An
independence-assuming interval would have made the first look precise.

**Determinism.** Rendered text depends on pretty-printer options and width. Unpinned,
you measure your own configuration drift and call it compiler drift.
`normalize.probe_options` tests each candidate option against the live toolchain
rather than trusting a hardcoded list — option names come and go, and `set_option` on
an unknown option is itself an error that would poison every file in the run. Apply
them with `-D` flags, not a prepended `set_option` block: prepending shifts every line
number and silently corrupts position data.

---

## Templating

Lean messages have a consistent shape:

```
<prose head>
  <indented pretty-printed term or goal state>
Hint: <trailer>
```

Tokenizing the payload is the mistake that makes template induction fail here — it is
an arbitrary term and will never generalize. `normalize.split_message` separates the
three parts; induction runs on the prose only, the payload becomes one hole typed by
coarse shape (`goal:1`, `goal:n`, `term:1`, `cases`, …), and trailers are tracked
separately so a message that gains a `Hint:` in some release does not fork its
template. Backquoted spans are single tokens, since Lean uses them to delimit
arguments and they are the highest-signal hole boundary available.

Current compression on `tests/elab_fail`: 1053 messages → 451 templates (2.33×).
There is headroom — `unexpected token {0}` and `unexpected token ' {0} '` should merge
and don't, because the quote characters block alignment. Lowering `THETA` fixes that
and costs purity; the labelled subset is how you pick the tradeoff instead of guessing.

---

## Layout

```
layer3/
  schema.py            RawDiagnostic → Realization → Observation; fingerprints
  normalize.py         option probing, scrubbing, prose/payload/trailer split
  template.py          alignment-based induction + self-evaluation
  weights.py           cascade collapse, stratified estimator, clustered bootstrap
  layer2_bind.py       declared contract for the Layer 1–2 join (many-to-many)
  corpora/expected_out.py   parser + git-only cross-tag differ
  drivers/harness.py        lean --json and LSP clients
containers/            one image per toolchain, matrix driver
data/                  measured error-name inventory
run.py                 harvest | drift | sweep | weigh
```

## Running

```bash
# What is in the test suite right now, and how much of it is tagged
python3 run.py harvest --repo /path/to/lean4 --tag master --stats

# Message drift between releases. No container, no elaboration — pure git plumbing.
# One `git cat-file --batch` per tag reads every expected file in one process.
python3 run.py drift --repo /path/to/lean4 --from v4.21.0 --to v4.22.0 > drift.ndjson

# Elaborate a package corpus under one toolchain
./containers/matrix.sh build v4.21.0 v4.22.0
./containers/matrix.sh sweep out/ v4.21.0 v4.22.0

# Weights
python3 run.py weigh out/obs-*.ndjson --top 50
```

Run **drift first over the whole tag range**, then use it to choose which toolchains
justify a sweep. Drift is seconds per tag pair; a sweep is a full elaboration of every
package. Exploiting that asymmetry is the difference between a matrix you can rerun
weekly and one you run once.

Stages are separate processes reading and writing NDJSON, so re-tuning the templater
or the estimator is a seconds-long replay over cached observations rather than a
rebuild of the matrix.

---

## Open seam

`layer2_bind.py` is a contract, not an implementation — I don't have your Layer 1–2
schemas. The one thing I'd argue for regardless of those schemas: model the join as an
**explicit many-to-many edge list with confidence**, not a dict. One Layer-2 node emits
several realizations (prose varying by argument shape), and one realization is emitted
by several nodes (`unknown identifier` comes from at least the elaborator, the
delaborator, and dot-notation resolution — same text, different cause). A dict
silently picks one and you never find out.

Edge sources in decreasing order of trust: `error_name` (exact, 8.1% coverage today,
growing), a versioned declared mapping (accurate, goes stale — `stale_against` tells
you which entries a bump invalidated), and instrumented message-construction sites
(the only route to full coverage, and worth the toolchain patch if this becomes
load-bearing).

To wire it up, tell me the Layer-2 node identifier shape and whether Layer 1 keys on
cause or on emission site — that determines whether `fingerprint → layer2_id` is the
right edge direction or whether it needs to go through an intermediate site table.
