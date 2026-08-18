# layer4 — free supervised repair labels from Lean/mathlib adaptation diffs

Layer 4 of an error-taxonomy pipeline. It mines toolchain-adaptation diffs from
mathlib4 history and turns each one into labelled `(broken, fixed)` repair
pairs plus **reversible breakage rules**.

The premise: a forward adaptation diff is already a repair. Read backwards it
is a *generator* — `post → pre` rewrites working code into code known to fail
under the new toolchain, with the correct repair attached. So one mined hunk
does not yield one training example; it yields a rule applicable at every
matching site in a current checkout.

Stdlib only. No third-party runtime dependencies.

---

## Reality check before you wire this in

Three things were verified against `leanprover-community/mathlib4` on
2026-07-28, and two of them contradict the obvious approach.

**1. `bump/v4.X.0` and `lean-pr-testing-NNNN` branches no longer exist.**
`git ls-remote --heads` returns 772 branches and **zero** matching either
pattern. Both are ephemeral and get deleted after use. Live infra branches now
use different conventions (`bump_to_v4.28.1`, `last_bump_for_v4.31.0`), and
`nightly-testing` is absent too. Branch-based mining alone will return nothing.

**2. Merge-topology mining also returns nothing.** mathlib4 master has **7
merge commits in its entire history**, all from 2021. Bump branches are
squash-merged.

**3. What *is* durable is the squashed commit on master.** A bump branch
collapses to a single commit like `chore: bump toolchain to v4.33.0-rc1
(#41779)` — 2433 files, 8421 insertions, 2018 deletions, one toolchain
transition. Discovery is therefore message- and toolchain-anchored on master,
with live branches as an opportunistic secondary source. On the real repo this
finds **131 windows** (88 bumps, 36 dependency adaptations, 7 backports), all
with resolved `toolchain_before` / `toolchain_after` pairs.

Discovery modes are independent and can be cross-checked against each other:

| mode | signal | use |
|---|---|---|
| `squash` | commit message regex | default; durable |
| `toolchain` | `lean-toolchain` blob changed | message-independent cross-check |
| `branch` | live ref name patterns | in-flight adaptations, if any exist |
| `merge` | merge topology | forks that really do merge (not mathlib4) |

---

## Install

```bash
pip install -e .          # or just put layer4/ on PYTHONPATH
python -m layer4 --help
```

Clone the corpus **with trees local**:

```bash
git clone --filter=blob:none --no-checkout --single-branch \
  --branch master https://github.com/leanprover-community/mathlib4.git
```

Do **not** use `--filter=tree:0`. A treeless clone makes every path-filtered
history walk lazily fetch one tree per commit; `git log -- lean-toolchain`
times out past 300s. `Git.check_clone_health()` warns about this and about
shallow clones.

---

## Pipeline

```bash
python -m layer4 discover --repo ./mathlib4 -o out/windows.jsonl
python -m layer4 mine     --repo ./mathlib4 -w out/windows.jsonl -o out/
python -m layer4 rules    --pairs out/pairs.jsonl -o out/rules.jsonl
python -m layer4 synth    --repo ./mathlib4 --rules out/rules.jsonl -n 5000
python -m layer4 replay   --repo ./mathlib4 --pairs out/pairs.jsonl --limit 200
python -m layer4 stats    --pairs out/pairs.jsonl
```

`discover → mine → rules` is free and offline-after-clone. `synth` multiplies
the corpus. `replay` is the only stage needing a real toolchain.

---

## Two label spaces, kept separate

**`RepairLabel`** — what the fix *did*. Derived from the diff alone, hence free.
**`ErrorClass`** — what the broken side is *predicted* to emit.

Every pair carries `expected_errors`. `replay` observes the real diagnostics and
scores agreement, so the free labels get a measured trust score rather than an
assumed one. Label ids are stable strings, not enum ordinals — remap them onto
your layers 1–3 taxonomy without touching the classifier.

Label families: `rename.*` (decl, namespace, attribute, open-scope, import,
deprecation), `elab.*` (defeq transparency, instance reducibility, ascription,
named args, universes, coercion, binder syntax), `auto.*` (simp-set add /
remove / reorient, tactic swap, term wrapper, proof rewrite), `perf.*`
(heartbeats, recursion depth, synth budget), `lint.*`, `meta.*`.

Classification is multi-label. `elab.instance_reducibility +
rename.attribute` and `rename.open_scope + rename.namespace` are the common
genuine pairings.

### Measured on the real v4.33.0-rc1 bump

8028 hunks, 24 labels, **0.15% residual** (7 `auto.proof_rewrite`, 5
`meta.unknown`). The catch-all bucket started at 38 hunks; three rules
(`open_scope_delta`, `lemma_list_fragment`, `inline_tactic_swap`) plus a
term-wrapper split reduced it to 7.

---

## The two things that will silently wreck the dataset

### Class collapse

That single bump is **81% one label** — mechanical `set_option
backward.isDefEq.respectTransparency[.types] false in` insertions — plus 14%
`@[implicit_reducible]` → `@[instance_reducible]`. Emitted raw, a repair model
learns to insert a `set_option` and nothing else.

`dedup` collapses identical edit shapes (whitespace- and comment-insensitive),
`--keep-per-signature N` retains N *contextually distinct* copies, and
`--cap-per-label` bounds the rest. Measured effect on that bump: 8075 raw →
496 unique, top-label share **81% → 30%**, across 353 files and 346
declarations. `duplicate_count` preserves the true frequency for reweighting.

### Leakage

The same rename appears in hundreds of hunks across hundreds of files, so a
random split puts near-duplicates on both sides. `--split-by` is group-disjoint
and defaults to `toolchain`, which additionally orders chronologically: train
on past bumps, test on the newest one. That matches deployment. Other keys:
`window`, `commit`, `file`, `decl`, `signature`.

Mining a single bump gives one toolchain group, which cannot fill three
buckets. Rather than silently write empty `val.jsonl` / `test.jsonl`, the
splitter walks down `toolchain → window → commit → decl → file → signature`
until a key yields at least three groups, and warns which one it used. If you
see that warning, mine more windows — a same-bump split is a much weaker
evaluation than a forward-in-time one.

---

## Reversal

`rules.jsonl` holds two rule strengths:

- **`substitution`** — a consistent 1-for-1 token mapping. Reversible and
  applicable anywhere the token occurs. High yield.
- **`window`** — a literal pre/post text slice. Applies only where the exact
  post text occurs. Low yield, high fidelity.

`synth` applies reversed rules to a clean checkout and **round-trip validates**
every sample: reverse then forward must restore the original byte-for-byte, or
the sample is dropped.

Two subtleties worth knowing, both found by inspecting bad output:

- Rule application uses `(?<![\w.'!?])name(?![\w'!?])(?!\.[A-Z])`. The trailing
  guard is load-bearing: without it a `Mathlib.Tactic` rule fires inside
  `Mathlib.Tactic.Common` and corrupts unrelated imports. A *lowercase* next
  component stays matchable, because `eqRec_heq_iff_heq.mp` really should be
  rewritten by a rule targeting the stem — `.mp` is a projection, not a
  namespace. Lean's UpperCamel-namespace convention separates the two, and the
  same convention distinguishes `rename.namespace` from `rename.decl`.
- Tokens in `_UNSAFE_REVERSE` (`rfl`, `simp`, `exact`, …) are never reversed;
  rewriting them produces breakage unrelated to the mined repair.

---

## Calibration loop

```bash
python -m layer4 replay --repo ./mathlib4 --pairs out/pairs.jsonl --limit 200
```

Checks out the post-adaptation tree at `toolchain_after`, applies the reverse
edit in situ, runs `lake env lean` on that one file, parses the diagnostics, and
compares observed against expected. `calibration.json` reports per label:

- `precision` — match / (match + mismatch). How much to trust that free label.
- `genuine_break_rate` — fraction where the reverse edit actually broke
  something. Pairs scoring `no_error` are **not repair instances** and should be
  dropped: the mined hunk was cosmetic or the breakage lived elsewhere.

Needs `elan`/`lake` and a warm cache, so it is sampled rather than exhaustive.
The diagnostic parser and calibration maths are unit-tested against synthetic
Lean output; the subprocess path is not exercised in CI here.

---

## `#adaptation_note` is the best signal in the corpus

mathlib marks toolchain workarounds in-source with a human-written sentence
naming the upstream change that forced them:

```lean
#adaptation_note
/-- `respectTransparency.types true` changes the auto-generated lemmas' signature -/
set_option backward.isDefEq.respectTransparency.types false in
```

247 of these were added in the v4.33.0-rc1 bump alone. They are natural-language
ground truth about *why* something broke — strictly better than anything
derivable from edit shape — and they are plumbed through to `notes` on both
pairs and rules. This is under-exploited; it is probably the highest-value
extension.

---

## Output schema

`pairs.jsonl`, one object per repair:

| field | meaning |
|---|---|
| `broken` / `fixed` | changed lines only, pre and post adaptation |
| `broken_window` / `fixed_window` | contiguous file slices incl. context; `fixed_window` appears verbatim in the post-adaptation file (asserted in tests) so reversal can string-match it |
| `label` / `labels` / `confidence` | primary, full multi-label set, rule confidence |
| `expected_errors` | predicted `ErrorClass` values; verified by `replay` |
| `evidence` | rule-specific detail — substitutions, simp deltas, set-options, attribute deltas, `notes` |
| `decl` / `decl_kind` / `namespace` | enclosing declaration, resolved through the namespace stack |
| `toolchain_before` / `toolchain_after` | e.g. `leanprover/lean4:v4.32.0` → `v4.33.0-rc1` |
| `window_kind` / `window_key` / `commit` / `pr` / `date` | provenance |
| `signature` / `duplicate_count` | dedup identity and true frequency |

`rules.jsonl`: `rule_id`, `kind`, `label`, `forward`, `support`, `files`,
`toolchains`, `provenance`, `notes`, `reversible`.

---

## Sample output

`sample_output/` contains real artifacts from a run against mathlib4:
`windows.jsonl` (all 131 discovered windows), a slice of `pairs.jsonl`, the top
induced rules, and the manifest with the label distribution.

## Tests

```bash
python tests/test_layer4.py     # 42 tests
```

Covers diff parsing, hunk splitting, per-side window reconstruction, the Lean
lexer and context scanner, every taxonomy rule, rule induction and reversal
(including the dotted-prefix regression), dedup/cap/split hygiene, and a full
`discover → mine → rules → synth` run against a real git repo built in
`setUp`.

---

## Gotchas encoded in the code

- **`Mathlib/**/*.lean` as a git pathspec silently drops every top-level file.**
  Git's default matcher is not wildmatch — that glob matches `Mathlib/Sub/B.lean`
  but not `Mathlib/A.lean`; only `:(glob)` magic gives the expected semantics.
  `mine.DEFAULT_PATHS` uses directory prefixes plus an extension filter. Worth
  checking whether layers 1–3 have the same bug.
- **Hunk splitting defaults to `gap=0`.** Git emits all `-` then all `+` for one
  edit, so intervening context means two independent edits; merging them yields
  a hunk with two unrelated labels. `--context` widens the diff, `gap` controls
  rejoining.
- **Hunk boundaries cut through attributes and simp lists.** `@[to_additive
  (attr := implicit_reducible,` and `simp only [a, b,` arrive unterminated;
  extraction tolerates this, and nested `attr := (...)` groups are flattened.
  Without that, edits get misfiled as plain identifier renames.
- **A closed tactic vocabulary is required.** Treating any lowercase line-head
  as a tactic labels 6052 `set_option ... in` insertions as `tactic_added`.
