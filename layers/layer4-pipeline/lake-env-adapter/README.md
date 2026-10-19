# Lake Environment Adapter

`lakeva` reconstructs a Lean project's dependency environment for an **exact target Lean toolchain**. It treats the old project's `lakefile` as the authority for direct dependencies, selects only those direct revisions manually, and delegates the transitive dependency graph to Lake.

It is designed for version-adaptation work where a dependency used by an old commit may have disappeared from a newer commit and therefore from the newer `lake-manifest.json`.

## Protocol implemented

For every direct dependency declared by the old project:

1. If the package exists in the new manifest, copy its complete resolved package record.
2. If it is absent:
   1. Ask Reservoir for a build whose `toolchain` is exactly the target toolchain and whose `built` field is `true`.
   2. Read that commit's `lean-toolchain` from the package repository and require an exact string match.
   3. If Reservoir has no verified candidate, inspect Git branch/tag tips and toolchain-change boundaries for a revision whose `lean-toolchain` matches exactly.
   4. If none exists, record the dependency as unresolved. No nearest-version guess is made.
3. Read the destination toolchain from the new project and write it to the target.
4. Write the target manifest in the layout of the new project's manifest (the target
   toolchain's Lake reads only that layout): the selected direct package records,
   converted if the old manifest used a different layout, followed by the new
   manifest's transitive records.
5. Update only `rev` declarations that already exist in the target `lakefile`.
6. Run `lake update --keep-toolchain <transitive>` so Lake reconstructs transitive
   dependencies while reusing the selected direct records. A bare `lake update` would
   re-resolve every direct dependency from the `lakefile` (for `@ "master"`, the current
   branch tip) and ignore the manifest; naming one transitive package makes Lake keep
   the other manifest records, and transitive records are re-resolved regardless. For
   older Lake versions that predate `--keep-toolchain`, retry without it (those
   versions have no toolchain update of their own). Lake runs with `ELAN_TOOLCHAIN`
   pinned to the target toolchain. If a dependency's post-update hook rewrote
   `lean-toolchain` (Mathlib's 2024 hook copies Mathlib's own toolchain over the root's
   and fetches its cache under that toolchain via `elan run`), the exact target
   toolchain is written back with a warning: Lake resolved and saved the manifest with
   the target toolchain before the hooks ran, so the result is unchanged.
7. Verify that Lake did not change any manually selected direct revision. A drift or
   failed Lake run restores all three edited files. A dependency declared under a Lake
   `meta if ... then` guard (for example `doc-gen4` behind `get_config? env = some
   "dev"`) is only a root dependency when that configuration is passed; its record
   being dropped by `lake update` is reported but is not a drift.

Local path dependencies are preserved from the old manifest because they do not have a remote revision to select.

## Install

Python 3.11 or newer and Git are required. Lake must be available when applying a plan unless `--skip-lake-update` is used.

```console
python -m pip install -e .
```

The install exposes both `lakeva` and `lake-env-adapter`.

### Windows without installation

The included `lakeva.cmd` runs directly from the source tree and automatically uses Codex's bundled Python when available. It can be called from any directory:

```cmd
"C:\Users\Cynthia\Documents\Codex\2026-09-17\i-x20\outputs\lake-env-adapter\lakeva.cmd" --version
```

This does not require `lakeva` or Python to be present on `PATH`.

## Typical workflow

Prepare three directories:

- `old`: the original project checkout whose direct dependencies must be retained;
- `new`: a checkout at the newer commit, with a manifest resolved for the target Lean version;
- `target`: the working copy being reconstructed. Its direct dependency set must match `old`; its toolchain may still be the old one because `lakeva` replaces it from `new`.

The normal workflow is now one command with positional paths:

```console
lakeva adapt ../project-old ../project-new ../project-adapted
```

This saves an audit plan at `TARGET/.lakeva-plan.json`, changes nothing if any dependency is unresolved, writes `NEW/lean-toolchain` into the target, applies direct revisions, and asks Lake to resolve transitives.

Use the separate `plan` and `apply` commands only when you want to review the decisions between phases.

Create a non-mutating plan:

```console
lakeva plan \
  --old ../pfr-v27 \
  --new ../pfr-v33 \
  --target ../pfr-v27-churn \
  --output lakeva-plan.json
```

On PowerShell, the same command can be written on one line:

```powershell
lakeva plan --old ..\project-old --new ..\project-new --target ..\project-adapted --output lakeva-plan.json
```

Review the summary or the complete JSON evidence:

```console
lakeva show lakeva-plan.json
lakeva show lakeva-plan.json --json
```

Apply the reviewed plan:

```console
lakeva apply lakeva-plan.json
```

By default, `apply`:

- refuses plans with unresolved dependencies;
- refuses if the target lakefile, manifest, or toolchain changed after planning;
- changes the target toolchain to the exact toolchain recorded in the plan;
- creates a timestamped backup under `TARGET/.lakeva-backup/`;
- restores the toolchain, lakefile, and manifest if Lake fails or changes a selected direct revision.

Use `--allow-unresolved` only when deliberately leaving those direct dependencies untouched. `--force` bypasses file-hash staleness checks, but never bypasses the exact toolchain check. `--skip-lake-update` patches direct entries without resolving transitives and is mainly useful for inspection or controlled testing.

## Supported Lake files

- `lakefile.toml` `[[require]]` entries, including Git, Reservoir, and path dependencies;
- declarative `lakefile.lean` `require` forms with string-literal sources and revisions;
- current flat manifests such as version `1.1.0`;
- older nested `{"git": {...}}` and `{"path": {...}}` package records.

`lakefile.lean` is executable Lean code. A general Python parser cannot safely evaluate arbitrary computed package names, URLs, or revisions, so `lakeva` intentionally fails closed when a `require` declaration uses a computed value it would need to edit. It never uses a regular expression to silently guess through an unsupported expression.

When both `lakefile.lean` and `lakefile.toml` exist, `lakefile.lean` takes precedence, matching Lake.

## Plan outcomes

Each direct dependency has one of four statuses:

- `selected`: a package record and exact revision were selected;
- `local-preserved`: an old local path record was retained;
- `unresolved`: no eligible exact-toolchain revision was found;
- `unsupported`: the available manifest record could not be used safely.

Sources are `new-manifest`, `reservoir`, `git-history`, `old-manifest`, or `none`. The JSON plan includes the evidence behind every decision and complete package records so `apply` does not repeat network resolution.

`plan` and `show` return exit code `2` when a plan contains unresolved/unsupported entries, while still writing or displaying the plan. User errors return `1`.

## Tests

The test suite has no third-party dependencies:

```console
python -m unittest discover -s tests -v
```

It covers both Lake config formats, flat and legacy manifest records, exact Reservoir checks, Git-history fallback, and the end-to-end plan/apply workflow.

## Trust and network note

Planning contacts Reservoir, GitHub raw content, and package Git repositories as needed. Applying invokes Lake, which evaluates the target project's Lake configuration and may download repositories. Use this tool only on projects whose Lake configuration you trust.
