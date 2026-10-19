# Lean version-transition build pipeline

This pipeline processes the 3,265 repositories in
`../lean-multi-toolchain-repos/qualifying-repositories.csv` and checkpoints every
repository, commit pair, native build, and adapted build.

The run phase processes different repositories concurrently. Pairs belonging to
the same repository remain serialized so Git worktrees and repository metadata
cannot race. The default is two workers.

## What it does

1. Reads every root `lean-toolchain` change on the current default branch.
2. For each newer version boundary, checks successively older boundaries until
   `git diff --name-only` finds a change outside:
   - `lean-toolchain`
   - `lake-manifest.json`
   - `lakefile.toml`
   - `lakefile.lean`
3. Records the selected old/new commit and exact toolchain strings. Fallback
   selection skips boundaries with the same toolchain as the newer commit.
   Identical-toolchain pairs and moving aliases such as `stable` or undated
   `nightly` are ineligible: their historical environment is not reproducible.
4. Creates detached `old` and `new` worktrees and runs each commit in its
   original toolchain:

   ```text
   lake exe cache get
   lake build
   ```

   The cache command is an optimization; native validity is determined by the
   build. Every cache/update/build process pins `ELAN_TOOLCHAIN` to the expected
   committed version; the toolchain file is checked before and after execution.
   When supported, every Lake command receives `--keep-toolchain`, so a
   missing manifest cannot silently switch the checkout to another Lean
   release. Old Lake versions are probed and run without that newer option.
   If Lake requests a manifest refresh, the pipeline runs `lake update` and
   retries without changing the committed toolchain. Supported Lake releases
   use verbose build output. Passing checks require successful completion,
   confirmed toolchain identity, and positive evidence of built Lean modules;
   a bare success banner is insufficient. New logs record root `.olean` counts.
   Explicitly optional binary-release download failures followed by source
   builds are not mistaken for required-job failures.
   Git submodules are initialized before the build.

   A compiler/project failure discards the pair. A timeout, interruption,
   network/download failure, dependency checkout failure, disk/memory problem,
   or path-length failure is instead recorded as `native_inconclusive`; it is
   never mistaken for evidence that the commit itself is broken. Inconclusive
   native checks are retried from a clean worktree before being paused. The
   resolved manifest produced by a passing native build is checkpointed and
   restored after build artifacts are cleaned, including when the manifest was
   not committed to Git.

   Paused `native_inconclusive` checks are kept out of adaptation. Retry them on
   a later run with `--retry-native-failures` after machine issues have been
   resolved. A native check whose required setup output shows a network failure
   is not recorded at all: the pair stays `pending` (see "Network failures").
5. Restores both passing worktrees to their exact commits, creates a clean target
   from the old commit, and runs:

   ```text
   lakeva adapt OLD NEW TARGET
   lake exe cache get
   lake build
   ```

6. Parses Lean errors. Each compiler diagnostic is written as its own JSON file
   under `parsed-errors/`, with the exact source line and four surrounding lines
   on each side when the source is available. Each file also contains the
   repository, old/new versions, old/new commits, and pair identity.
7. Writes `pairs-without-lean-errors.jsonl`, with one record only for every pair
   whose adapted build completed successfully, compiled at least one target,
   and produced zero Lean diagnostics. Failed, empty, interrupted, and
   infrastructure-only builds are excluded.

An environment-adapter failure is recorded in the progress database and its raw
log remains under `state/runs/`; it is not a Lean compiler error and therefore is
not written to `parsed-errors/`.

Build output is saved directly as `build.log`. Failed infrastructure attempts
are retained alongside it in the native-check directory. Retries preserve old
logs under that run/check's `history/` directory. Diagnostic source files are
snapshotted before temporary checkouts are removed, so reparsing retains the
actual adapted source, including generated files and dependency sources.

On Windows, disposable worktrees default to a short, state-specific directory
under `%TEMP%\lvp` and every Lake-spawned Git process receives
`core.longpaths=true`. This avoids the dependency checkout failures caused by
the much deeper state-directory path and keeps separate pipeline state files
from sharing temporary checkouts.

## Throughput and disk usage

Measured on the first ~650 processed pairs, native validation of the two
endpoints took ~85% of the run time, yet only ~13% of eligible pairs produced
Lean diagnostics. The run phase therefore defaults to **target-first**:

1. Check out old/new (no build), adapt the old commit, and build the target.
2. Only if the adapted build produced Lean diagnostics (or with
   `--validate-clean-pairs`) are the old and new commits built natively, with
   the built target removed first to free disk. Errors are exported only after
   both native checks pass, exactly as before. If a native build had to
   refresh a stale committed manifest, the adapted build is repeated in that
   exact environment.
3. A clean adapted build without native validation is recorded as
   `unvalidated` and is not written to `pairs-without-lean-errors.jsonl`. Run
   once with `--validate-clean-pairs` to complete those pairs.

When either commit has no `lake-manifest.json`, the pair falls back to the old
native-first order, because the manifest only exists after a native build.
`--native-first` restores the old order for every pair.

Pending pairs are run in order of expected yield: forward bumps of 1-3 Lean
minor releases first, then larger jumps/downgrades, then nightly or non-semver
toolchains, and last same-minor (patch/rc) transitions and pairs whose diff
touches no `.lean` file (none of the latter produced a Lean error so far).
`--skip-low-yield` drops the last group. Pairs with equal priority are grouped
by toolchain, so installs are reused. `--target-error-files N` stops
scheduling new pairs once `parsed-errors/` holds N files.

Pairs whose old commit is Mathlib itself (a fork with `Mathlib.lean` and
`Mathlib/Tactic` at its root) are marked `ineligible_pair / mathlib-itself`
before any checkout: adapting them means compiling all of Mathlib without a
cache, which exceeds the build timeout. `--include-mathlib` requeues them.

A native build that hits its time limit is no longer retried (it will hit the
limit again), and the default `--native-timeout`/`--build-timeout` is one
hour. `lake exe cache get` runs only when Mathlib is in the manifest.

Disk usage is bounded between pairs:

- `--max-toolchains 12`: elan keeps every toolchain it installs (0.5-2.5 GB
  each). The least recently used pipeline toolchains beyond this count are
  removed; elan reinstalls one on demand. The elan default, elan overrides,
  `--protect-toolchain` values, toolchains of running pairs, and those of the
  next few scheduled pairs are never removed. Toolchains that never occur in
  the pair table are never touched. `0` disables this.
- `--mathlib-cache-max-gb 15`: the oldest `.ltar` files in Mathlib's download
  cache are pruned, never while a `cache get` is running. `0` disables this.
- On Windows, `state/runs` and `state/native-checks` are NTFS-compressed once
  (new logs inherit it); `--no-compress-logs` disables this. The final native
  attempt log is moved, not copied, to its stable name.

### Local dependency mirrors

Lake clones Mathlib (a 525 MB pack) and a handful of small dependencies from
GitHub into every old, new, and target worktree. The pipeline therefore keeps
local bare mirrors under `--dep-mirror-root` (default
`%USERPROFILE%\.cache\lvp-dep-mirrors`, on the same volume as `%TEMP%` so
clones hardlink the packs instead of copying them) and seeds each worktree
from them before Lake runs. Only these URLs are mirrored (matched after
lowercasing and stripping a trailing `/` or `.git`): mathlib4, batteries,
std4, aesop, quote4, import-graph, lean4-cli, plausible, LeanSearchClient.
ProofWidgets is never mirrored because its `preferReleaseBuild` download reads
the remote URL; every other dependency is left to Lake.

A mirror is created on first use (`git clone --bare`, tracking every branch
and tag but not GitHub's pull-request refs), refreshed once at the start of
every run, and refreshed on demand when a manifest revision is missing from it
(`git fetch --prune`, then a fetch by hash). A per-mirror lock keeps two
workers from fetching the same mirror at once. Seeding reads the worktree's
`lake-manifest.json` (manifest versions 4-7 and the `1.x.0` string versions,
with the `packagesDir` the manifest names) and, for every allowlisted package
whose directory does not exist yet, clones from the mirror with `--no-checkout`,
sets `origin` to the manifest's exact URL string, and detaches `HEAD` at the
manifest's revision. Lake then finds an up-to-date checkout whose remote
matches and does nothing (`updateGitRepo` in Lake's `Load/Materialize.lean`);
lakeva's `lake update <pkg>` reuses the same directory and fetches only the
delta. Seeding happens in every native-check attempt and in the target before
`lakeva adapt`; old/new trees used only as adaptation sources are not seeded.
Each seed is logged (`# seeded: <name> <rev> from mirror ...`) in
`seed-attempt-NN.log` of the native check or `seed.log` of the run. Any mirror
failure (including a network failure, which is reported to the network
watchdog) only logs `# not seeded`, removes the partial directory, and lets
Lake clone as before; it never fails the pair. Mirrors are never deleted by
the janitors; `--status` shows their count and size. `--no-dep-mirror`
disables all of this.

### Background worktree deletion

Removing a multi-GB target worktree idled a worker for 5-8 minutes after a big
build. Finished worktrees are now renamed into `<worktrees-root>\.trash\`
(an instantaneous same-volume rename; `git worktree remove --force` then only
drops the metadata) and deleted by a single daemon janitor thread. If the
rename fails, the worktree is deleted inline as before. Before starting a new
pair, a worker waits while more than `--trash-max-gb` (default 20) of
discarded data is still queued. After a normal completion the run waits for
the janitor to finish; after an interruption the remaining trash is left in
place and swept at the next startup (inline when `--no-async-cleanup` is
set, which disables the background deletion entirely). `git worktree prune`
runs per pair as before.

### Reuse across forks

19% of queued pairs are fork duplicates: another repository has a pair with the
same `(old_hash, new_hash, old_version, new_version)`. Building the same
commits under the same toolchains gives the same result, so the pipeline
copies instead of rebuilding (`--no-fork-reuse` disables this):

- Native checks: when `(repo, commit)` has no row, a `passed` row of another
  repository for the same commit and toolchain that still satisfies the
  evidence rules and has its manifest snapshot, or a `failed` row with its
  build log, is copied: the log directory (logs and `lake-manifest.json`) is
  copied into this repository's native-check directory with a
  `provenance.json` (`source_repo`, `source_commit`, `source_log_dir`,
  `copied_at`), and the row is inserted with the same status and exit codes.
  Inconclusive results are never copied this way.
- Pairs: before any worktree is created, a finished twin that is `completed`
  (`build-errors` or `build-succeeded`), `unvalidated`, or `adapt_failed`
  (`lakeva-adapt-failed`, `adapted-submodule-setup-failed`) at the current
  result version, whose run directory still holds `build.log` (and, for
  build-errors, `diagnostic-sources.json` plus every exported error file), is
  copied into this pair's run directory with `provenance.json`
  (`source_pair_id`, `source_repo`). This pair's error files are regenerated
  from the copied log and source snapshot, so they carry this repository,
  pair id, pair index, hashes, and versions; the number of files must equal
  the source's export count. The repair diff is taken from this repository's
  own clone. For `completed` twins both native rows must exist for this
  repository (via the native reuse above); otherwise nothing is copied and the
  pair is processed normally. `unvalidated` twins are not reused when
  `--validate-clean-pairs` is set. The pair row is updated as
  `finish_adapted_pair` would (`last_stage='reused-from-fork'`, exit codes,
  result version, error export). Inconclusive, network-failure,
  `pipeline_failed`, and `discarded_native` results are never reused.
- Scheduling: a pair whose key is currently running in another worker is
  deferred so the later one can reuse the first's result.

Reused pairs count like any other in `pairs-without-lean-errors.jsonl` and
`--status` (which also prints the number of reused pairs).

### Skipping builds that are certain to time out

`--no-timeout-prediction` disables both parts. The results stay retryable
with `--retry-native-failures` / `--retry-build-inconclusive`.

1. Duplicate timeouts. If another repository's native check of the same
   commit and toolchain is `inconclusive` with build exit code 124, or a twin
   pair ended `build_inconclusive / adapted-build-incomplete` with exit code
   124, this one is recorded the same way without running the build: the
   `build.log` holds only `# skipped: duplicate of <repo>/<commit or pair>
   timed out`, the source path, and `# exit_code: 124`, next to a
   `provenance.json` (`last_stage='skipped-duplicate-timeout'` for pairs).
2. ProofWidgets cascade. All one-hour timeouts on Lake < v4.16 with Mathlib
   happened when ProofWidgets had to be rebuilt, which invalidates every cached
   Mathlib artifact. After a successful `lake exe cache get`, and only when
   the toolchain is a release or rc below v4.16.0 (nightlies are not
   predicted), the manifest names both `mathlib` and `proofwidgets`, and
   `lake --help` lists `--no-build` (Lake v4.8+), the pipeline runs
   `lake build +ProofWidgets.Compat --no-build` with a 300 s limit, logged to
   `predict.log`. Lake's no-build exit code (3) means ProofWidgets is out of
   date: the pair is recorded as `build_inconclusive / predicted-build-timeout`
   (the native check as `inconclusive`, its pair as `native_inconclusive /
   predicted-build-timeout`) and the build is skipped. Exit 0 and every other
   exit code build normally. `--repair-results` keeps these records.

### Repair diffs and finished clones

For every pair with Lean errors, the old->new commit diff (excluding `.lake/`)
is saved as `repair-diffs/<repo>__<pair_id>.diff.gz`. The error reports hold
only the broken code; this diff is the candidate repair for the repair corpus.
Missing diffs are backfilled at the start of each run.

`--delete-finished-repos` deletes a repository clone once none of its pairs is
pending, running, `native_inconclusive`, `build_inconclusive`, or
`pipeline_failed`, and none of its pairs produced Lean errors. Clones with
error pairs are kept, because minimizing a repair means rebuilding the old
commit with selected diff hunks applied; clones with paused or failed pairs are
kept because the `--retry-*` flags need them. Deleted repositories are marked
in the database: discovery keeps their saved pairs, and their pairs are skipped
by later runs until `--restore-deleted-clones` re-clones every deleted
repository that still has such pairs, using the manifest's URL and clone mode,
and resets its `clone_deleted` flag. Pairs left pending by `--skip-low-yield`
keep their clone.

### Network failures

The clones are blobless, so even `git worktree add` fetches file contents from
GitHub. A failure whose output shows that the network or the remote was
unreachable (`Could not resolve host`, `Failed to connect`, promisor fetch
failures, download errors, ...) during worktree creation, `lakeva adapt`, or a
native check is not a result about the pair: the pair is returned to `pending`
with outcome `network-failure` and is retried by the next run without any flag.
No native-check row is stored for a network-interrupted check.

Repeated network failures pause the run instead of marking every queued pair.
After `--network-failure-limit` consecutive failures (default 5) the pipeline
connects to `github.com`; if that fails, the run stops exactly as after an
interruption, with exit code 75, and the affected pairs stay `pending`. If
`github.com` is reachable, the failures concerned individual remotes and the
count restarts. `0` disables the pause.

Paths Git cannot create on this platform (Windows device names such as
`Aux.lean` or `Con.lean`, `:` or `?` in a name) are recorded as
`ineligible_pair / invalid-path-on-platform`; they can only be processed on a
different operating system.

The first run of this version converts stored results that recorded only a
network outage: such `pipeline_failed`, `adapt_failed`, inconclusive adapted
builds, and inconclusive native checks become `pending / network-failure`, and
invalid-path failures become `ineligible_pair`.

The first run after this change performs the initial trim before any build
starts, which can take a while on a machine with many installed toolchains.

## Safety

Lake configuration is executable code. Running this across public GitHub
repositories executes untrusted code with your user account's permissions. Use
an isolated machine or sandbox. The run phase refuses to start unless you pass
`--acknowledge-untrusted-builds`.

## Run

From PowerShell:

```powershell
cd C:\Users\Cynthia\Documents\Codex\2026-09-20\i-w\outputs\lean-version-build-pipeline

# Discover and checkpoint all version pairs without executing Lake code.
.\run_pipeline.ps1 --phase discover

# Run native checks, adaptation, builds, and parsing.
.\run_pipeline.ps1 --phase run --acknowledge-untrusted-builds
```

Tune concurrency for the machine:

```powershell
# Recommended starting point; this is also the default.
.\run_pipeline.ps1 --phase run --workers 2 --acknowledge-untrusted-builds

# On a machine with ample RAM, CPU, and fast storage.
.\run_pipeline.ps1 --phase run --workers 4 --acknowledge-untrusted-builds

# Preserve the previous fully sequential behavior.
.\run_pipeline.ps1 --phase run --workers 1 --acknowledge-untrusted-builds
```

Each Lake build can itself use several cores, so increasing `--workers` too far
can make the run slower through memory pressure and disk contention. Start with
two; compare four only if the machine remains responsive.

Or perform both phases in one invocation:

```powershell
.\run_pipeline.ps1 --acknowledge-untrusted-builds
```

Re-run the identical command after interruption. Any pair that was `running` is
automatically returned to `pending`; completed work and native checks are reused.
Parallel interruption terminates active child processes and returns their pairs
to `pending` before exit.

The first run of a repaired version backs up the database and exported files in
`state/backups/`. It invalidates older native results whose evidence fails the
current validation rules and requeues affected pairs. It also
re-parses completed build logs when the report format changes, removing stale
or malformed error files. Passing native checks and completed adapted analyses
that still satisfy the stricter evidence rules remain cached.

Inspect progress without building:

```powershell
.\run_pipeline.ps1 --status
```

Status is read-only, including while a run is active. A process-level lock
rejects a second run or repair sharing the same state directory.

Repair saved outputs without running builds:

```powershell
.\run_pipeline.ps1 --repair-results
```

Repair excludes questionable historical results instead of labeling them as
successes or source failures. Missing/unverifiable native evidence is requeued;
ambiguous adapted evidence is marked `build_inconclusive`. No compiler output is
invented for excluded results, and the backup retains the previous reports.

Useful controls:

```powershell
# Safe small discovery test
.\run_pipeline.ps1 --phase discover --repo HuangZhiming-the-Great/math

# Execute one pair from one repository
.\run_pipeline.ps1 --phase run --repo HuangZhiming-the-Great/math --max-pairs 1 --acknowledge-untrusted-builds

# Re-clone deleted repositories that still have pending or retryable pairs
.\run_pipeline.ps1 --phase discover --restore-deleted-clones

# Retry orchestration failures (not genuine adapted-build errors)
.\run_pipeline.ps1 --phase run --retry-failed --acknowledge-untrusted-builds

# Retry pairs whose Lake environment adaptation previously failed
.\run_pipeline.ps1 --phase run --retry-adapt-failures --acknowledge-untrusted-builds

# Recheck adapted builds that lacked reliable evidence or hit machine failures
.\run_pipeline.ps1 --phase run --retry-build-inconclusive --acknowledge-untrusted-builds

# Reconsider genuine failures and inconclusive native checks again
.\run_pipeline.ps1 --phase run --retry-native-failures --acknowledge-untrusted-builds

# Skip cache downloads; native/adapted builds still run
.\run_pipeline.ps1 --skip-cache --acknowledge-untrusted-builds
```

Throughput features are on by default and can be switched off individually:
`--no-dep-mirror` (and `--dep-mirror-root PATH`), `--no-async-cleanup` (and
`--trash-max-gb N`), `--no-fork-reuse`, `--no-timeout-prediction`; see
"Throughput and disk usage".

Timeout values are seconds and can be changed with `--cache-timeout`,
`--native-timeout`, `--adapt-timeout`, and `--build-timeout`. The number of
clean attempts for an inconclusive native check is controlled by
`--native-attempts` (default: 2). Override the short temporary checkout root
with `--worktrees-root` only if needed.

## Layout

```text
lean-version-build-pipeline/
  commit_to_version.py       pair discovery
  extract_lean_errors.py     Lean/Lake diagnostic parser
  run_pipeline.py            resumable orchestrator
  resource_management.py     yield-ordered scheduler, toolchain/cache limits
  run_pipeline.ps1           Windows launcher
  parsed-errors/             final JSON error reports only
  pairs-without-lean-errors.jsonl
                             analyzed pairs with zero Lean diagnostics
  repair-diffs/              gzip old->new diffs of pairs with Lean errors
  state/
    progress.sqlite3         durable progress database
    pairs/                   selected adjacent-version pairs
    native-checks/           original-toolchain cache/build logs
                             (provenance.json marks results copied from a fork)
    runs/                    lakeva, cache, predict, seed, and adapted-build logs
  %TEMP%/lvp/<state-id>/     short temporary worktrees; removed after each pair
    .trash/                  finished worktrees awaiting background deletion
  %USERPROFILE%/.cache/lvp-dep-mirrors/
                             bare mirrors of Mathlib and common dependencies
```

`parsed-errors/` is intentionally kept separate from raw logs and state. A
successful adapted build creates no file there. Native-build, adapter, and
non-diagnostic process failures also create no file; their state and logs remain
under `state/`. Existing pair-level wrapper reports are converted automatically
the next time the pipeline starts.

The no-error JSONL deliberately excludes pending pairs, native-invalid or
native-inconclusive pairs, adapter failures, orchestration failures, empty or
infrastructure-only builds, and pairs that produced a Lean error.
It is regenerated atomically after each pair, at startup, normal completion, and interruption,
so rerunning the pipeline does not create duplicate entries. Override its path
with `--no-lean-errors-output` if needed.

Only the repository's configured default build targets are tested; this is not
a guarantee that every `.lean` file or project test was executed. Warnings,
including `sorry` warnings, are not Lean errors.

## Environment adapter (lakeva)

The pipeline's adaptation step calls `lakeva adapt OLD NEW TARGET`. The adapter
lives in `lake-env-adapter/` (Python package `lake_env_adapter`, launcher
`lakeva.cmd`, tests under `lake-env-adapter/tests`). Put that directory on
`PATH` or pass `--lakeva <path>\lakeva.cmd`. Its own README documents the
dependency-selection protocol.

## Dataset scripts

- `build_finetuning_dataset.py` combines `parsed-errors/` into the diagnostic
  chat corpus `lean-errors-finetune.jsonl`.
- `build_repair_corpus.py` turns error pairs into verified minimal repairs
  (`repair-corpus.jsonl`) by re-adapting the old commit and minimizing the
  author's diff hunks against `lake build`.
- `qc_and_split.py` applies the quality checks and the repository-grouped and
  transition-held-out splits.
- `normalize_diagnostics.py` normalizes diagnostics and groups them into
  Drain-style templates.
- `lpma_colab_finetune_compare.py` runs the QLoRA fine-tuning and baseline
  comparison on a Colab T4.

## Verification

`python -m unittest discover -s tests -v` runs offline regression tests for
parsing, classification, migration, concurrency, locking, resumption,
dependency mirrors and seeding (with local Git repositories), background
worktree deletion, fork reuse, and timeout prediction (with a fake `lake`).
`python tests/smoke_real_pipeline.py` additionally runs real Git/Lake/lakeva
checks on three small local repositories (compatible transition, API break,
and broken native commit). It requires installed Lean v4.0.0 and v4.31.0 and
the local adapter path recorded in that test.

`python audit_results.py` is a read-only comparison of exported records against
saved logs, native evidence, source-context shape, and database classifications.
Run it while the pipeline is stopped for a consistent snapshot.

## Output example

```json
{
  "file": "Project/File.lean",
  "line": 42,
  "column": 7,
  "message": "unknown identifier ...",
  "source_found": true,
  "error_line": "  exact source line containing the error",
  "code": "four lines before\n...\nerror line\n...\nfour lines after",
  "context_start_line": 38,
  "context_end_line": 46,
  "repo": "owner/repository",
  "old_version": "leanprover/lean4:v4.19.0",
  "new_version": "leanprover/lean4:v4.20.0",
  "old_hash": "...",
  "new_hash": "...",
  "pair_index": 0,
  "pair_id": "..."
}
```
