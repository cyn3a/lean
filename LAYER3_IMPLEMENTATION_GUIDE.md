# Layer 3 — Detailed Implementation and Operations Guide

This guide covers both ways of “implementing” Layer 3:

1. deploying and running the supplied `mathlib-repair-harvester` 0.2.0 release; and
2. understanding or extending the implementation in another codebase.

Layer 3 has one data flow:

```text
Lean 4 expected-output history ──> canonical diagnostics + fixture transitions ─┐
Reservoir build history ─────────> structured replay jobs ─────────────────────┤
Mathlib tag history ─────────────> three-state replay jobs ────────────────────┤
                                                                                v
                                                                    structured harness
                                                                                |
                                                   diagnostics + realizations + transitions
                                                                                |
                                                                                v
                                                              empirical frequency weights
```

The central rule is strict: diagnostic text is accepted only from structured Lean output, never scraped from human-formatted `lake build` logs.

---

## 1. Choose the host environment

### Recommended on Windows

Use Ubuntu under WSL2 and expose Docker through Docker Desktop’s WSL integration. Run all commands below inside the Ubuntu terminal, not PowerShell. This avoids the usual parade of path-conversion, executable, and bind-mount problems.

### Recommended on Linux

Use a normal Linux host with Docker Engine.

### Host requirements

For the default Docker-backed Layer 3 pipeline:

- Python 3.11 or later;
- `git`;
- Docker;
- enough disk space for Git mirrors, detached worktrees, Lake dependencies, caches, and multiple Lean toolchains;
- network access to GitHub, the elan installer, Lean toolchain releases, Lake dependencies, and Reservoir when API mode is used.

The host does **not** need elan for the default Docker engine. The container bootstrap installs elan and the exact toolchain for each job. Host elan is required only for `--engine local-elan` and for the original non-Layer-3 validator.

A typical Ubuntu setup is:

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv git curl jq unzip

# Install Docker using your preferred supported method, then verify:
docker version
docker run --rm ubuntu:24.04 true
```

Do not continue until the Docker smoke test succeeds. The harness cannot negotiate with a daemon that is absent, stopped, or offended by your group permissions.

---

## 2. Obtain and verify the release

The release contains:

- `mathlib-repair-harvester-layer3-0.2.0.zip`: complete source tree, documentation, tests, schemas, and wheel;
- `mathlib_repair_harvester-0.2.0-py3-none-any.whl`: installable wheel;
- `mathlib-repair-harvester-layer3-0.2.0-SHA256SUMS.txt`: release checksums.

Expected SHA-256 values:

```text
c53b36e96b2128eb178518d173a7c7bb8d15192c77338df91641a096ede164fe  mathlib-repair-harvester-layer3-0.2.0.zip
e8c64ac374f30cb37744c36159d812cc09251fdbb70b2d05f5ad7723c306db32  mathlib_repair_harvester-0.2.0-py3-none-any.whl
```

Verify them from the directory containing all three files:

```bash
sha256sum -c mathlib-repair-harvester-layer3-0.2.0-SHA256SUMS.txt
```

Both lines should report `OK`.

---

## 3. Install the package

### Option A: editable source installation

Use this when changing the implementation.

```bash
unzip mathlib-repair-harvester-layer3-0.2.0.zip
cd mathlib-repair-harvester-layer3-0.2.0

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

Run the test suite:

```bash
python -m unittest discover -s tests -v
```

Run the CLI smoke test:

```bash
mathlib-repair-harvest layer3 --help
mathlib-repair-harvest layer3 core-tests --help
mathlib-repair-harvest layer3 reservoir --help
mathlib-repair-harvest layer3 mathlib-history --help
mathlib-repair-harvest layer3 run --help
mathlib-repair-harvest layer3 weights --help
```

### Option B: wheel installation

Use this for an immutable deployment.

```bash
python3 -m venv layer3-venv
source layer3-venv/bin/activate
python -m pip install --upgrade pip
python -m pip install ./mathlib_repair_harvester-0.2.0-py3-none-any.whl
mathlib-repair-harvest layer3 --help
```

The Python package itself has no third-party runtime dependencies.

---

## 4. Create a clean workspace

Keep repositories, planned corpora, realized runs, and final weights separate.

```bash
mkdir -p "$HOME/lean-layer3"/{repos,empirical,realized,logs}
cd "$HOME/lean-layer3"

export L3_ROOT="$PWD"
export REPOS="$L3_ROOT/repos"
export EMPIRICAL="$L3_ROOT/empirical"
export REALIZED="$L3_ROOT/realized"
```

Recommended layout:

```text
lean-layer3/
  repos/
    lean4/
    mathlib4/
  empirical/
    core-tests/
    reservoir/
    mathlib-history/
    frequency-weights/
  realized/
    combined/
      harness/
  logs/
```

Do not point multiple independent harness executions at the same `--output` directory. `run` writes, rather than appends, `harness/*.jsonl`. Either merge the job files and run once, or use separate output roots.

---

## 5. Clone the versioned repositories

```bash
git clone --filter=blob:none https://github.com/leanprover/lean4.git "$REPOS/lean4"
git clone --filter=blob:none https://github.com/leanprover-community/mathlib4.git "$REPOS/mathlib4"

git -C "$REPOS/lean4" fetch --tags --force
git -C "$REPOS/mathlib4" fetch --tags --force
```

A partial clone retains history while downloading historical blobs on demand. A full clone is also valid and is preferable for completely offline replay after the initial fetch.

Inspect available tags before choosing a pilot window:

```bash
git -C "$REPOS/lean4" tag --list 'v4.*' --sort=version:refname | tail -n 20
git -C "$REPOS/mathlib4" tag --list 'v4.*' --sort=version:refname | tail -n 20
```

### Ref-selection semantics

Layer 3 supports:

- repeated `--ref REF`: preserves exactly the order supplied;
- `--refs-file FILE`: one ref per line, comments beginning with `#` ignored;
- repeated `--tag-pattern PATTERN`: appends matching tags in Git version order;
- `--include-head`: appends `HEAD`;
- `--max-refs N`: keeps only the most recent `N` resolved refs.

Duplicate commits are removed. With no refs, no tag pattern, and no `--include-head`, the planner uses `HEAD` only. One ref can produce a snapshot corpus, but it cannot produce adjacent-ref transitions.

For a controlled research run, prefer explicit immutable tags or commit SHAs. Avoid `HEAD` in the final dataset.

---

## 6. Implement Corpus A: Lean 4 core expected-output history

### What this stage does

This stage does **not** execute Lean. It mines versioned expected-output fixtures already committed to the Lean 4 repository.

Recognized expected-output conventions are:

```text
*.lean.expected.out
*.lean.out.expected
*.lean.expected
*.out.expected   # only when paired with a tracked .lean source file
```

The parser accepts one or more JSON values separated only by whitespace. It extracts diagnostics from:

- LSP `PublishDiagnosticsParams` objects;
- JSON-RPC `textDocument/publishDiagnostics` notifications;
- JSON-RPC result wrappers;
- raw diagnostic arrays;
- interactive structured messages containing `append`, `tag`, `expr`, widget fallback text, or trace nodes.

Any file containing prose or malformed JSON is written to `rejections.jsonl`, not partially mined.

### Pilot command

Start with two or three explicit refs:

```bash
mathlib-repair-harvest layer3 core-tests \
  --repository "$REPOS/lean4" \
  --repository-name leanprover/lean4 \
  --ref v4.20.0 \
  --ref v4.21.0 \
  --output "$EMPIRICAL"
```

Replace the example tags with tags that actually exist in your clone.

### Broader command

```bash
mathlib-repair-harvest layer3 core-tests \
  --repository "$REPOS/lean4" \
  --tag-pattern 'v4.*' \
  --max-refs 20 \
  --output "$EMPIRICAL"
```

### Output files

```text
empirical/core-tests/
  refs.jsonl
  documents.jsonl
  diagnostics.jsonl
  transitions.jsonl
  rejections.jsonl
  summary.json
```

Inspect them:

```bash
jq . "$EMPIRICAL/core-tests/summary.json"
wc -l "$EMPIRICAL/core-tests/"*.jsonl
head -n 1 "$EMPIRICAL/core-tests/diagnostics.jsonl" | jq .
head -n 1 "$EMPIRICAL/core-tests/transitions.jsonl" | jq .
head -n 1 "$EMPIRICAL/core-tests/rejections.jsonl" | jq .
```

### How transition diffing works

For each underlying `.lean` test path and each adjacent ref pair:

1. exact `signature_id` values are matched as a multiset;
2. unmatched diagnostics with the same severity, code, file/URI, and range anchor are paired as `changed`;
3. residual old diagnostics become `removed`;
4. residual new diagnostics become `added`;
5. repeated identical diagnostics are preserved through the event `count` field.

The underlying Lean test path, rather than the expected-file suffix, is the comparison key. Therefore a migration from `Foo.lean.expected.out` to `Foo.lean.out.expected` does not look like deleting and recreating the test.

### Important interpretation

`document_count` counts accepted structured expected-output documents, not every expected-output fixture in Lean. Many core fixtures intentionally contain ordinary program output or prose. Their rejection is expected and should be audited by directory rather than treated as parser failure by default.

---

## 7. Implement Corpus B: Reservoir package and build history

This stage normalizes structured package metadata and emits replay jobs. It supports exactly one source mode per invocation.

### Mode A: selected package API records

Best for a pilot:

```bash
mathlib-repair-harvest layer3 reservoir \
  --package leanprover/cslib \
  --package leanprover-community/physlib \
  --job-selection failed \
  --backend lean-json \
  --max-files 50 \
  --output "$EMPIRICAL"
```

For each package, the adapter requests structured metadata, versions, and builds from the Reservoir API.

### Mode B: full generated manifest

```bash
mathlib-repair-harvest layer3 reservoir \
  --manifest /absolute/path/to/reservoir-manifest.json \
  --job-selection changed \
  --backend lean-json \
  --max-files 100 \
  --output "$EMPIRICAL"
```

The manifest must either be a package array or contain a top-level `packages` array.

### Mode C: local Reservoir index

```bash
mathlib-repair-harvest layer3 reservoir \
  --index /absolute/path/to/reservoir/index \
  --job-selection changed \
  --backend lean-json \
  --max-files 100 \
  --output "$EMPIRICAL"
```

The local index is expected to contain paths of the form:

```text
<owner>/<package>/metadata.json
<owner>/<package>/versions.json   # optional
<owner>/<package>/builds.json     # optional
```

### Job-selection modes

- `failed`: include builds where `built == false` or `tested == false`;
- `changed`: include failures, `requiredUpdate == true`, and build/test outcome changes within a package revision;
- `all`: include every build record.

The default is `failed`.

### Output files

```text
empirical/reservoir/
  packages.jsonl
  versions.jsonl
  builds.jsonl
  jobs.jsonl
  summary.json
```

Inspect the result:

```bash
jq . "$EMPIRICAL/reservoir/summary.json"
head -n 1 "$EMPIRICAL/reservoir/jobs.jsonl" | jq .
```

A replay job is omitted when any essential replay field is missing:

- repository URL;
- source revision;
- exact toolchain.

Therefore `build_count > 0` and `job_count == 0` can be valid. Inspect the normalized build and package records before assuming the adapter has betrayed you personally.

---

## 8. Implement Corpus C: Mathlib history and three-state plans

### What this stage does

For each adjacent selected ref pair, the planner compares changed Lean files. By default it keeps only transitions where `lean-toolchain` changed.

A changed file that exists in both states, including a rename, yields three jobs:

```text
old-control:
  old source revision + old toolchain

counterfactual-broken:
  old source revision + new toolchain

new-fixed:
  new source revision + new toolchain
```

This isolates the toolchain intervention from the source repair.

Pure additions and deletions remain in transition metadata but generate no fabricated three-state experiment, because one side has no source file.

### Pilot command

```bash
mathlib-repair-harvest layer3 mathlib-history \
  --repository "$REPOS/mathlib4" \
  --repository-name leanprover-community/mathlib4 \
  --ref v4.20.0 \
  --ref v4.21.0 \
  --max-files 20 \
  --backend lean-json \
  --output "$EMPIRICAL"
```

### Broader command

```bash
mathlib-repair-harvest layer3 mathlib-history \
  --repository "$REPOS/mathlib4" \
  --tag-pattern 'v4.*' \
  --max-refs 10 \
  --max-files 200 \
  --backend lean-json \
  --output "$EMPIRICAL"
```

Use `--include-same-toolchain` when source changes between adjacent refs are relevant even though the `lean-toolchain` string did not change.

### Output files

```text
empirical/mathlib-history/
  refs.jsonl
  transitions.jsonl
  jobs.jsonl
  summary.json
```

Inspect them:

```bash
jq . "$EMPIRICAL/mathlib-history/summary.json"
head -n 1 "$EMPIRICAL/mathlib-history/transitions.jsonl" | jq .
jq -r '.role' "$EMPIRICAL/mathlib-history/jobs.jsonl" | sort | uniq -c
```

### Why a transition may generate no jobs

Common causes are:

- the selected refs use the same toolchain and `--include-same-toolchain` was not set;
- no `.lean` files changed;
- all Lean changes are pure additions or deletions;
- `lean-toolchain` is missing in the relevant ref;
- `--max-files` was set to a non-positive or unexpectedly restrictive value after manual editing.

---

## 9. Choose the diagnostic backend

### `lean-json`

The harness runs:

```text
lake env lean --json <file.lean>
```

Use this for the main corpus unless LSP-specific behavior is a research target.

Properties:

- simpler process model;
- one JSON object required per non-empty stdout line;
- non-JSON stdout causes rejection;
- nonzero exit with structured error diagnostics is accepted as a structured failed run;
- nonzero exit with no structured JSON is rejected;
- stderr is hashed and counted but never mined.

### `lsp` or `collectDiagnostics`

These names select the same current backend. The harness starts:

```text
lake serve -- -DstderrAsMessages=false -Dexperimental.module=true
```

It then:

1. sends `initialize`;
2. sends `initialized`;
3. opens the document at version 1;
4. sends `textDocument/waitForDiagnostics` for that URI and version;
5. accumulates and merges incremental `publishDiagnostics` notifications;
6. stops at the matching response;
7. sorts diagnostics deterministically;
8. closes the document, shuts down, and exits.

Use LSP for editor-faithful diagnostics, LSP ranges, and diagnostic behavior that differs from direct frontend invocation. It is slower and has more protocol failure modes.

### Recommended experimental design

- use `lean-json` for the full corpus;
- run an LSP audit sample on the same files;
- compare exact signatures, templates, severity, and missing/extra diagnostic rates;
- do not mix backends without retaining `source_format` and backend metadata.

---

## 10. Review and safely modify jobs

A job minimally needs:

```json
{
  "schema_version": 1,
  "job_id": "job:...",
  "corpus": "mathlib-history",
  "repository_path": "/absolute/path/to/mathlib4",
  "revision": "<commit-sha>",
  "toolchain": "leanprover/lean4:v4.x.y",
  "backend": "lean-json",
  "discover_lean_files": false,
  "files": ["Mathlib/Some/File.lean"],
  "max_files": 20,
  "setup": {
    "lake_update_if_missing_manifest": true,
    "fetch_cache": true,
    "prebuild": true
  }
}
```

Inspect all toolchains and job sizes:

```bash
jq -r '.toolchain' "$EMPIRICAL/mathlib-history/jobs.jsonl" | sort | uniq -c
jq -r '[.job_id, .role, (.files | length), .max_files] | @tsv' \
  "$EMPIRICAL/mathlib-history/jobs.jsonl" | head -n 30
```

### Critical provenance rule

`job_id` is content-derived at planning time. If you manually change a job’s revision, toolchain, backend, files, subdirectory, setup, or limit, delete the old `job_id`. The harness recomputes it only when the field is absent.

Example: lower every job to 10 files and recompute IDs at runtime:

```bash
jq -c 'del(.job_id) | .max_files = 10' \
  "$EMPIRICAL/mathlib-history/jobs.jsonl" \
  > "$EMPIRICAL/mathlib-history/jobs.pilot.jsonl"
```

Keeping the old ID after changing the intervention creates false provenance. Computers are literal creatures; they will not infer your moral intentions.

---

## 11. Combine job sources before one harness run

The safest approach is one combined job file:

```bash
cat \
  "$EMPIRICAL/mathlib-history/jobs.jsonl" \
  "$EMPIRICAL/reservoir/jobs.jsonl" \
  > "$EMPIRICAL/all-jobs.jsonl"

wc -l "$EMPIRICAL/all-jobs.jsonl"
```

Optionally validate that every line is JSON:

```bash
python - <<'PY'
import json
from pathlib import Path
p = Path.home() / "lean-layer3/empirical/all-jobs.jsonl"
for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
    if line.strip():
        json.loads(line)
print("valid JSONL")
PY
```

If you run sources separately, use separate roots such as:

```text
realized/mathlib/
realized/reservoir/
```

Do not reuse `realized/combined` for two independent invocations unless overwriting the first run is genuinely the intended scientific method.

---

## 12. Execute the Docker/elan harness

### First pilot

```bash
mathlib-repair-harvest layer3 run \
  --jobs "$EMPIRICAL/all-jobs.jsonl" \
  --output "$REALIZED/combined" \
  --engine docker \
  --base-image ubuntu:24.04 \
  --file-timeout 900 \
  --setup-timeout 7200
```

### Debugging run

```bash
mathlib-repair-harvest layer3 run \
  --jobs "$EMPIRICAL/all-jobs.jsonl" \
  --output "$REALIZED/debug" \
  --engine docker \
  --base-image ubuntu:24.04 \
  --keep-containers \
  --keep-checkouts \
  --file-timeout 900 \
  --setup-timeout 7200 \
  --fail-fast
```

### Local elan development mode

```bash
elan --version
mathlib-repair-harvest layer3 run \
  --jobs "$EMPIRICAL/all-jobs.jsonl" \
  --output "$REALIZED/local" \
  --engine local-elan
```

This still uses detached source worktrees, but toolchains and process state are on the host. Use it for development, not as the default isolation claim.

### What the Docker engine does

For each exact toolchain string, it:

1. derives a deterministic container name from workspace, base image, and toolchain;
2. creates one persistent container with the harness workspace mounted at `/work`;
3. installs `curl`, `git`, build tools, compression utilities, and certificates;
4. installs elan;
5. installs the exact requested toolchain;
6. records a marker file so it does not reinstall that toolchain in the same kept container;
7. invokes commands through `elan run <toolchain> ...`;
8. removes containers at the end unless `--keep-containers` is set.

The current bootstrap assumes a Debian/Ubuntu-style image with `bash` and `apt-get`. Passing an Alpine or distroless image to `--base-image` will fail unless the engine bootstrap is modified.

### What the checkout manager does

For each source repository, it:

1. creates or updates one bare mirror under `workspace/repositories/`;
2. creates one detached Git worktree per job under `workspace/checkouts/`;
3. checks out the exact job revision;
4. applies the package `subdir` if present;
5. removes the worktree after the job unless `--keep-checkouts` is set.

Toolchain containers are reused, but source states and local `.lake` directories are not shared across jobs.

---

## 13. Understand setup behavior

For a Lake project, the default generated jobs request:

```text
lake update                  # only when manifest is missing
lake exe cache get
lake build
```

Each setup record stores:

- command;
- exit code;
- timeout flag;
- duration;
- stdout byte count and SHA-256;
- stderr byte count and SHA-256.

It deliberately does not store or parse setup prose as diagnostics.

A nonzero setup command is recorded but does not immediately abort the job. The file-level diagnostic invocation still runs. Therefore inspect `setup.jsonl`; a later file failure may be downstream of a failed cache fetch or prebuild.

For a repository without `lakefile.lean` or `lakefile.toml`, setup is skipped. The standard backends still invoke `lake`, so such a repository generally requires a custom job/backend or a Lake wrapper.

---

## 14. Inspect harness outputs

```text
realized/combined/harness/
  toolchains.jsonl
  setup.jsonl
  runs.jsonl
  diagnostics.jsonl
  rejections.jsonl
  jobs.jsonl
  job_failures.jsonl
  realizations.jsonl
  transitions.jsonl
  summary.json
```

Initial inspection:

```bash
jq . "$REALIZED/combined/harness/summary.json"
wc -l "$REALIZED/combined/harness/"*.jsonl
head -n 1 "$REALIZED/combined/harness/runs.jsonl" | jq .
head -n 1 "$REALIZED/combined/harness/diagnostics.jsonl" | jq .
head -n 1 "$REALIZED/combined/harness/job_failures.jsonl" | jq .
```

### File-run success semantics

For `lean-json`, a file succeeds only when:

- output is valid strict JSON lines;
- the process did not time out;
- the process return code is zero.

For LSP, a file succeeds when the collection protocol completes and the canonical diagnostic set contains zero errors.

### Job success semantics

A job succeeds when:

- at least one file was executed; and
- every file run succeeded.

An empty discovered file list therefore produces an unsuccessful job rather than a vacuous success.

### Three-state realization semantics

A Mathlib transition is `valid_pass_fail_pass` only when:

```text
old-control             succeeds
counterfactual-broken   fails
new-fixed               succeeds
```

Inspect:

```bash
jq -c 'select(.valid_pass_fail_pass == true)' \
  "$REALIZED/combined/harness/realizations.jsonl" | head

jq -r '[.transition_id, .old_control_pass, .counterfactual_broken_fail, .new_fixed_pass, .valid_pass_fail_pass] | @tsv' \
  "$REALIZED/combined/harness/realizations.jsonl" | head -n 30
```

### Transition generation

For valid or invalid three-role groups, the harness compares diagnostic signature multisets:

- signatures in broken but not old control become `added` events at `old-control-to-counterfactual-broken`;
- signatures in broken but not new fixed become `removed` events at `counterfactual-broken-to-new-fixed`.

The event `count` preserves multiplicity.

---

## 15. Understand the canonical diagnostic schema

Every canonical diagnostic contains:

```text
observation_id
signature_id
message_fingerprint
template_fingerprint
severity
code
message
message_template
source_format
file / uri
range / full_range
tags
raw
metadata
```

### Identifier meanings

- `observation_id`: one occurrence in a document/snapshot/range;
- `signature_id`: hash of severity, code, and exact normalized message;
- `message_fingerprint`: hash of exact normalized message;
- `template_fingerprint`: hash of the conservative message template.

The signature intentionally excludes file and range, allowing the same diagnostic type to aggregate across documents. The observation ID includes provenance and location.

Repeated identical diagnostics at the same location receive distinct observation IDs through an occurrence ordinal, so multiplicity is not silently collapsed.

### Position normalization

LSP coordinates are already zero-based. CLI `lean --json` positions are converted from one-based lines to zero-based lines. Columns are kept as the provided character/column value.

### Message normalization

Exact normalization:

- normalizes line endings;
- collapses horizontal whitespace per line;
- removes trailing spaces;
- collapses three or more blank lines to two.

Template normalization additionally abstracts:

- URLs;
- absolute `.lean` paths;
- long hexadecimal hashes;
- generated metavariable names;
- quoted Lean-like identifiers;
- standalone numerals.

Do not casually make the template regex more aggressive. Over-collapsing distinct error mechanisms makes frequency numbers prettier and research conclusions worse, a trade humans somehow keep making.

---

## 16. Compute frequency weights

Combine core fixture diagnostics with realized harness diagnostics and both transition sources:

```bash
mathlib-repair-harvest layer3 weights \
  --diagnostics "$EMPIRICAL/core-tests/diagnostics.jsonl" \
  --diagnostics "$REALIZED/combined/harness/diagnostics.jsonl" \
  --transitions "$EMPIRICAL/core-tests/transitions.jsonl" \
  --transitions "$REALIZED/combined/harness/transitions.jsonl" \
  --alpha 0.5 \
  --output "$EMPIRICAL"
```

Output:

```text
empirical/frequency-weights/
  signature_weights.jsonl
  template_weights.jsonl
  totals.json
  summary.json
```

Inspect:

```bash
jq . "$EMPIRICAL/frequency-weights/summary.json"
jq . "$EMPIRICAL/frequency-weights/totals.json"
head -n 1 "$EMPIRICAL/frequency-weights/signature_weights.jsonl" | jq .
head -n 1 "$EMPIRICAL/frequency-weights/template_weights.jsonl" | jq .
```

### Weight definitions

For type `t`, corpus `c`, count `n[c,t]`, corpus total `N[c]`, vocabulary size `V`, and smoothing parameter `alpha`:

```text
p_bal(t) = average over corpora c of:
           (n[c,t] + alpha) / (N[c] + alpha * V)
```

The release emits this as both:

```text
corpus_balanced_probability
frequency_weight
```

Other outputs are:

```text
global_probability
  = (n[t] + alpha) / (N + alpha * V)

inverse_document_frequency
  = ln((D + 1) / (df[t] + 1)) + 1

surprisal_bits
  = -log2(global_probability)

transition_event_share
  = transition occurrences involving t / all transition occurrences
```

Weights are computed separately for exact `signature_id` and `template_fingerprint`.

A changed event contributes at most once to a given key’s participation count, even when before and after collapse to the same template. Directional `transition_changes_from` and `transition_changes_to` remain separate.

### How to use the fields

- training prior reflecting empirical prevalence: `frequency_weight`;
- rare-event stratification: `inverse_document_frequency` or `surprisal_bits`;
- migration analysis: additions, removals, directional changes, and `transition_event_share`;
- robustness against one generated fixture repeating an error many times: use `document_frequency`, not only `occurrence_count`.

The tool intentionally does not combine frequency and rarity into one magic score.

---

## 17. Quality-control checks

### Basic integrity

```bash
python - <<'PY'
import json
from pathlib import Path

root = Path.home() / "lean-layer3"
paths = [
    root / "empirical/core-tests/diagnostics.jsonl",
    root / "empirical/core-tests/transitions.jsonl",
    root / "realized/combined/harness/diagnostics.jsonl",
    root / "realized/combined/harness/transitions.jsonl",
    root / "empirical/frequency-weights/signature_weights.jsonl",
    root / "empirical/frequency-weights/template_weights.jsonl",
]
for path in paths:
    count = 0
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            json.loads(line)
        except Exception as exc:
            raise SystemExit(f"{path}:{line_no}: {exc}")
        count += 1
    print(path, count)
PY
```

### Observation-ID uniqueness

```bash
python - <<'PY'
import json
from collections import Counter
from pathlib import Path

paths = [
    Path.home() / "lean-layer3/empirical/core-tests/diagnostics.jsonl",
    Path.home() / "lean-layer3/realized/combined/harness/diagnostics.jsonl",
]
ids = []
for path in paths:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            ids.append(json.loads(line)["observation_id"])
duplicates = [k for k, n in Counter(ids).items() if n > 1]
print("observations:", len(ids))
print("duplicate observation IDs:", len(duplicates))
if duplicates:
    raise SystemExit(1)
PY
```

### Operational gates

Review at minimum:

```text
harness summary:
  failed_job_count
  rejection_count
  structured_run_count versus run_count
  valid_pass_fail_pass_count

job failures:
  repository cloning failures
  missing revisions
  missing subdirectories
  invalid toolchain names
  toolchain installation failures

setup records:
  failed lake update
  failed cache get
  failed prebuild

core rejections:
  malformed JSON versus intentionally non-diagnostic output
```

### Optional JSON Schema validation

The source tree contains:

```text
schema/empirical-diagnostic.schema.json
schema/diagnostic-transition.schema.json
schema/harness-job.schema.json
schema/frequency-weight.schema.json
```

Install an optional validator:

```bash
python -m pip install jsonschema
```

Then validate a JSONL file with a small Python script using `jsonschema.Draft202012Validator`. Keep this optional dependency outside the core package if a zero-dependency runtime remains a requirement.

---

## 18. Reproducibility protocol

For a publishable corpus, retain:

- source repository URLs;
- exact commit SHAs, not merely tag names;
- `refs.jsonl` for every corpus;
- original `jobs.jsonl` used for execution;
- exact Docker base image, preferably pinned by digest;
- exact package release checksum;
- all harness summaries, setup digests, rejections, and job failures;
- schemas and package version;
- the smoothing `alpha` used for weights;
- a hash of every final JSONL file.

Recommended final hashing:

```bash
find "$EMPIRICAL" "$REALIZED/combined/harness" -type f -print0 \
  | sort -z \
  | xargs -0 sha256sum \
  > "$L3_ROOT/final-manifest.sha256"
```

Use immutable refs for final data. A tag is useful metadata; the resolved commit is the reproducible boundary.

---

## 19. Scaling from pilot to production

### Pilot

- 2–3 refs;
- 1–2 Reservoir packages;
- `--max-files 10` or `20`;
- `lean-json` backend;
- keep checkouts only during debugging.

### Medium run

- 5–10 refs;
- Reservoir `failed` or `changed` selection;
- `--max-files 100`;
- normal cleanup;
- inspect all setup failures before expansion.

### Full run

- immutable ref list frozen before execution;
- base image pinned by digest;
- adequate disk monitoring;
- one combined jobs file or explicitly separated output roots;
- periodic checkpoint copies of completed output directories;
- no changes to normalization after test-set inspection without versioning the schema or template algorithm.

The current harness processes jobs sequentially. Blindly running many harness processes against the same workspace can duplicate containers, compete for Git worktrees, and make cache behavior difficult to interpret. Shard only after giving each process a separate workspace/output root and recording the shard manifest.

---

## 20. Troubleshooting

### `docker: permission denied`

Confirm the daemon is running and the current user can access it:

```bash
docker version
docker run --rm ubuntu:24.04 true
```

Fix Docker access before retrying. Layer 3 is not a Docker support séance.

### `no Git refs resolved`

```bash
git -C "$REPOS/lean4" fetch --tags --force
git -C "$REPOS/lean4" tag --list 'v4.*' --sort=version:refname | tail
```

Check spelling and repository choice. Use explicit commit SHAs when tags differ between repos.

### Core `rejection_count` is large

Inspect paths and reasons. Many expected-output files are intentionally non-JSON. The accepted corpus is deliberately narrower than the entire Lean test-output corpus.

### Reservoir `job_count` is zero

Check:

```bash
jq -r '[.package, .revision, .toolchain, .built, .tested, .required_update] | @tsv' \
  "$EMPIRICAL/reservoir/builds.jsonl" | head -n 50

jq -r '[.full_name, .repository_url] | @tsv' \
  "$EMPIRICAL/reservoir/packages.jsonl" | head -n 50
```

The selected builds may not match the mode, or essential replay metadata may be absent.

### Mathlib `job_count` is zero

Check toolchain changes and replayable pairs:

```bash
jq -r '[.from_ref, .from_toolchain, .to_ref, .to_toolchain, .lean_change_count, .replayable_file_count] | @tsv' \
  "$EMPIRICAL/mathlib-history/transitions.jsonl"
```

Use `--include-same-toolchain` only when scientifically intended.

### `non-JSON line in Lean --json output`

A wrapper, plugin, or imported code wrote prose to stdout. The run is rejected rather than partially parsed. Inspect byte hashes and reproduce the exact command manually in a kept checkout/container. Consider the LSP backend for an audit, but do not weaken strict JSON parsing merely to improve acceptance rate.

### Nonzero setup command

Inspect `setup.jsonl` by job ID:

```bash
jq -c 'select(.returncode != 0 or .timed_out == true)' \
  "$REALIZED/combined/harness/setup.jsonl" | head -n 50
```

A cache helper may be absent, a dependency may have moved, or the package may need custom setup. Setup failure does not automatically stop file execution.

### LSP timeout or fatal file-processing error

- raise `--file-timeout`;
- confirm `lake serve` starts manually;
- inspect the project’s build state;
- compare with the `lean-json` backend;
- retain the checkout and container for exact reproduction.

### Job has zero files

Check `subdir`, file discovery, explicit file paths, and `max_files`. A zero-file job is unsuccessful.

### Output from a previous run disappeared

The same `--output` root was reused. Run once with a combined jobs file or use separate output roots.

---

## 21. Code-level implementation map

The source modules are deliberately separated by responsibility.

```text
mathlib_repair_harvester/cli.py
  argparse command surface and dispatch

mathlib_repair_harvester/git_refs.py
  ordered ref resolution, toolchain extraction, snapshot IDs

mathlib_repair_harvester/core_tests.py
  expected-file discovery, structured fixture parsing, adjacent-ref diffs

mathlib_repair_harvester/reservoir.py
  manifest/index/API loading, package/version/build normalization, job selection

mathlib_repair_harvester/mathlib_history.py
  changed-file pairing and three-role job planning

mathlib_repair_harvester/harness.py
  Docker/local engines, mirrors, worktrees, setup, file runs, realizations

mathlib_repair_harvester/lsp_client.py
  Content-Length framing, initialize/open/wait/merge/close/shutdown protocol

mathlib_repair_harvester/structured_diagnostics.py
  strict JSON decoding, structured-message flattening, normalization, IDs

mathlib_repair_harvester/frequency.py
  occurrence/document/corpus/transition aggregation and weights

schema/*.schema.json
  public data contracts

tests/*.py
  synthetic Git, Reservoir, strict JSON, LSP, harness, and frequency tests
```

---

## 22. Reimplementing Layer 3 in another system

Follow this order. Starting with orchestration before fixing the data contracts is how pipelines become museums of accidental assumptions.

### Step 1: define public schemas

Define four records first:

1. canonical diagnostic;
2. diagnostic transition;
3. harness job;
4. frequency-weight row.

Require schema versions and stable identifiers. Allow corpus-specific metadata inside `metadata` or additional transition/job fields, but keep the canonical diagnostic top level strict.

### Step 2: implement strict structured decoding

Provide two decoders:

```python
parse_concatenated_json(text)
parse_json_lines(text)
```

The first allows multiple JSON values separated only by whitespace. The second requires one complete JSON value per non-empty line. Neither may skip malformed fragments.

### Step 3: flatten Lean structured messages

Recursively extract semantic text from:

```text
text
append
compose
tag
expr
goal
alt
message
msg
trace
widget.alt
```

Ignore RPC references, JavaScript hashes, ranges, URLs in widget metadata, and implementation props. Retain the raw object separately.

### Step 4: canonicalize diagnostics

Normalize:

- severity;
- code;
- exact message;
- template message;
- zero-based positions;
- file and URI;
- tags.

Compute separate hashes for occurrence, signature, exact message, and template. Preserve repeated occurrences instead of deduplicating them.

### Step 5: implement the core fixture indexer

For every selected ref:

```python
tracked_files = git_ls_tree(ref)
expected_files = recognize_expected_suffixes(tracked_files)
blob_texts = batch_read_blobs(expected_files)
for expected_file in expected_files:
    values = strict_parse(blob_text)
    diagnostics = canonicalize(values, snapshot_metadata)
```

Batch blob reads to avoid one Git subprocess per fixture.

Then compare adjacent snapshots by underlying Lean test path using multiset signature matching before changed/add/remove pairing.

### Step 6: implement the Reservoir adapter

Support:

- manifest input;
- local index input;
- selected API packages.

Normalize package source, package subdirectory, versions, revisions, exact toolchains, build/test outcomes, and timestamps. Emit jobs only when repository, revision, and toolchain are known.

### Step 7: implement Mathlib three-state planning

For each adjacent ref pair:

```python
changes = git_diff_name_status(old, new)
for changed Lean file:
    determine old path, new path, old existence, new existence
    if both exist:
        emit replayable pair
```

Create old-control, counterfactual-broken, and new-fixed jobs. Keep additions/deletions in provenance without inventing missing states.

### Step 8: implement toolchain isolation

Separate toolchain isolation from source isolation:

- one persistent environment per exact toolchain;
- one detached source checkout per job;
- all commands invoked through the exact toolchain selector;
- no cross-job `.lake` state in source trees.

A `ToolchainEngine` interface needs only:

```python
ensure(toolchain)
command(toolchain, cwd, args, interactive=False)
server_path(host_path)
close()
```

### Step 9: implement the direct JSON backend

Execute:

```text
lake env lean --json file.lean
```

Accept nonzero return codes when structured diagnostics exist, because those are genuine failed examples. Reject nonzero exits without a structured message. Never parse stderr prose as diagnostics.

### Step 10: implement the LSP backend

Implement Content-Length framing and a deterministic state machine. Do not use a fixed sleep. Synchronize with `textDocument/waitForDiagnostics`, merge incremental diagnostics, and handle fatal file-progress notifications.

### Step 11: implement realizations

Group job summaries by transition and role. A valid repair realization is exactly:

```text
old success AND broken failure AND fixed success
```

Use multiset subtraction of signature counts to derive diagnostics introduced by breakage and removed by the fix.

### Step 12: implement frequency aggregation

For signatures and templates independently, record:

- occurrences;
- document sets;
- snapshot sets;
- repository sets;
- per-corpus occurrences and documents;
- transition additions, removals, and directional changes.

Compute global smoothed probability, equal-mass corpus-balanced probability, IDF, surprisal, and transition share. Version the weighting definition when changing it.

### Step 13: add synthetic tests

At minimum test:

- expected-file suffix migration;
- concatenated JSON values;
- prose rejection;
- duplicate diagnostics at one location;
- zero-based CLI position conversion;
- Reservoir failed-build selection;
- Mathlib three-role generation;
- no invented state for added/deleted files;
- strict JSON-lines behavior;
- framed LSP incremental merge;
- Docker path and exact-toolchain command construction;
- corpus-balanced frequency math;
- transition share under template collapse.

### Step 14: expose the CLI only after the contracts pass

The command layer should be thin. Corpus adapters and the harness should be directly callable functions, allowing tests to bypass shell parsing.

---

## 23. Recommended end-to-end pilot script

Save as `run-pilot.sh`, replace the example refs, and run inside the activated virtual environment.

```bash
#!/usr/bin/env bash
set -euo pipefail

L3_ROOT="${L3_ROOT:-$HOME/lean-layer3}"
REPOS="$L3_ROOT/repos"
EMPIRICAL="$L3_ROOT/empirical"
REALIZED="$L3_ROOT/realized"

mkdir -p "$REPOS" "$EMPIRICAL" "$REALIZED"

if [[ ! -d "$REPOS/lean4/.git" ]]; then
  git clone --filter=blob:none https://github.com/leanprover/lean4.git "$REPOS/lean4"
fi
if [[ ! -d "$REPOS/mathlib4/.git" ]]; then
  git clone --filter=blob:none https://github.com/leanprover-community/mathlib4.git "$REPOS/mathlib4"
fi

git -C "$REPOS/lean4" fetch --tags --force
git -C "$REPOS/mathlib4" fetch --tags --force

# Replace with existing adjacent refs chosen before the experiment.
OLD_REF=v4.20.0
NEW_REF=v4.21.0

mathlib-repair-harvest layer3 core-tests \
  --repository "$REPOS/lean4" \
  --ref "$OLD_REF" \
  --ref "$NEW_REF" \
  --output "$EMPIRICAL"

mathlib-repair-harvest layer3 mathlib-history \
  --repository "$REPOS/mathlib4" \
  --ref "$OLD_REF" \
  --ref "$NEW_REF" \
  --max-files 20 \
  --backend lean-json \
  --output "$EMPIRICAL"

mathlib-repair-harvest layer3 reservoir \
  --package leanprover/cslib \
  --job-selection failed \
  --backend lean-json \
  --max-files 20 \
  --output "$EMPIRICAL"

cat \
  "$EMPIRICAL/mathlib-history/jobs.jsonl" \
  "$EMPIRICAL/reservoir/jobs.jsonl" \
  > "$EMPIRICAL/all-jobs.jsonl"

mathlib-repair-harvest layer3 run \
  --jobs "$EMPIRICAL/all-jobs.jsonl" \
  --output "$REALIZED/combined" \
  --engine docker \
  --base-image ubuntu:24.04 \
  --file-timeout 900 \
  --setup-timeout 7200

mathlib-repair-harvest layer3 weights \
  --diagnostics "$EMPIRICAL/core-tests/diagnostics.jsonl" \
  --diagnostics "$REALIZED/combined/harness/diagnostics.jsonl" \
  --transitions "$EMPIRICAL/core-tests/transitions.jsonl" \
  --transitions "$REALIZED/combined/harness/transitions.jsonl" \
  --alpha 0.5 \
  --output "$EMPIRICAL"

jq . "$EMPIRICAL/core-tests/summary.json"
jq . "$EMPIRICAL/mathlib-history/summary.json"
jq . "$EMPIRICAL/reservoir/summary.json"
jq . "$REALIZED/combined/harness/summary.json"
jq . "$EMPIRICAL/frequency-weights/summary.json"
```

The pilot is complete when the summaries, rejections, setup failures, and three-state realizations have been inspected manually. A successful command exit is not, by itself, a research-quality dataset. Software remains tragically unable to peer-review its operator.
