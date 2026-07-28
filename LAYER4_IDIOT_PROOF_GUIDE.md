# Layer 4 for absolute beginners

This guide assumes you use **Windows 10 or Windows 11**. You do not need to understand Docker, Lean toolchains, Git internals, or JSONL before starting.

The whole pipeline has only three meaningful operations:

1. **Mine:** find repair diffs written by Mathlib maintainers.
2. **Run:** reverse each repair under the newer Lean toolchain, producing broken code and structured compiler errors.
3. **Export:** pair the broken state and its diagnostics with the original repair patch.

The final product is a JSONL file where each line is one supervised repair example.

---

## The idea in one tiny example

Suppose a newer Lean version renamed `oldLemma` to `newLemma`.

A Mathlib maintainer fixes this:

```diff
- exact oldLemma
+ exact newLemma
```

Layer 4 keeps the **new Lean version**, reverses that source patch, and obtains:

```lean
exact oldLemma
```

The new compiler now reports that `oldLemma` is unknown. Layer 4 stores:

```text
INPUT:  broken Lean code + compiler diagnostic
TARGET: patch changing oldLemma to newLemma
```

That is the free supervised label.

---

# Part A. Install the two Windows programs

## Step 1. Install Ubuntu through WSL

Do this in **Windows PowerShell**, not Ubuntu.

1. Open the Windows Start menu.
2. Type `PowerShell`.
3. Right-click **Windows PowerShell**.
4. Click **Run as administrator**.
5. Paste:

```powershell
wsl --install -d Ubuntu
```

6. Restart the computer when Windows asks.
7. Open **Ubuntu** from the Start menu.
8. Ubuntu asks for a Linux username and password. Choose anything you can remember.

When Ubuntu asks for a password, nothing appears while you type. The keyboard is working. Linux simply enjoys making new users doubt reality.

From now on, every command in this guide goes into the **Ubuntu terminal**, unless the guide explicitly says PowerShell.

### Check WSL

In Windows PowerShell, run:

```powershell
wsl -l -v
```

You want Ubuntu to show version `2`.

If it shows version `1`, run:

```powershell
wsl --set-version Ubuntu 2
```

---

## Step 2. Install Docker Desktop

1. Install **Docker Desktop for Windows**.
2. During installation, choose the **WSL 2** backend when offered.
3. Open Docker Desktop from the Start menu.
4. Accept its terms.
5. Open **Settings** in Docker Desktop.
6. Under **General**, enable **Use the WSL 2 based engine** if that option is visible.
7. Open **Resources → WSL Integration**.
8. Turn on integration for **Ubuntu**.
9. Click **Apply**.
10. Leave Docker Desktop running.

Do **not** run `sudo apt install docker.io` inside Ubuntu. This guide uses Docker Desktop's WSL integration, and installing a second Docker engine inside Ubuntu can create conflicts.

### Check Docker

Open Ubuntu and paste:

```bash
docker run --rm hello-world
```

Success means you see text containing:

```text
Hello from Docker!
```

Do not continue until that works.

---

# Part B. Download the starter bundle

Download and unzip:

```text
layer4-idiot-proof-starter.zip
```

The unzipped folder contains:

```text
LAYER4_IDIOT_PROOF_GUIDE.md
run_layer4_pilot.sh
mathlib_repair_harvester-0.3.0-py3-none-any.whl
```

Keep those three files together.

---

# Part C. Run the automatic pilot

## Step 3. Find the downloaded folder from Ubuntu

Most browsers put downloads in your Windows `Downloads` folder.

In Ubuntu, paste:

```bash
cd /mnt/c/Users
ls
```

You will see one or more Windows usernames. Enter yours. Replace `YOUR_WINDOWS_NAME` below:

```bash
cd /mnt/c/Users/YOUR_WINDOWS_NAME/Downloads
ls
```

Find the unzipped starter folder, then enter it. For example:

```bash
cd layer4-idiot-proof-starter
ls
```

You should see the three files listed above.

If the folder name contains spaces, put quotation marks around it:

```bash
cd "Layer 4 Starter"
```

---

## Step 4. Start the script

Still in Ubuntu, paste:

```bash
chmod +x run_layer4_pilot.sh
./run_layer4_pilot.sh
```

The script will ask for your Ubuntu password while installing a few basic packages. Again, no characters appear while typing the password.

The script then automatically:

1. checks Docker;
2. installs the Layer 4 Python package;
3. creates `~/lean-layer4` inside Ubuntu;
4. clones the correct repository;
5. fetches all remote adaptation branches;
6. mines a small pilot corpus;
7. makes reverse-broken and forward-fixed jobs;
8. runs them in exact Lean toolchain containers;
9. collects only structured diagnostics;
10. exports validated supervised examples.

Keep Docker Desktop open throughout the run.

The script stores the repository under the Ubuntu filesystem, not under `C:\`. This avoids needlessly slow Linux development work over the Windows-mounted filesystem.

---

# Part D. Find the results

## Step 5. Locate the newest run

In Ubuntu, paste:

```bash
LATEST=$(find ~/lean-layer4/runs -mindepth 1 -maxdepth 1 -type d | sort | tail -n 1)
echo "$LATEST"
```

The printed directory is the complete run.

Show the final summary:

```bash
jq . "$LATEST/dataset/supervised-repair-labels/summary.json"
```

The most important fields are:

```text
validated_count
error_validated_count
diagnostic_only_count
rejected_count
validation_rate
```

A validation rate below 100% is normal. Some historical diffs are refactors, incomplete adaptations, setup failures, or changes whose reversed form does not create a useful diagnostic.

---

## Step 6. Choose the correct output file

### Best file for error-repair training

```text
$LATEST/dataset/supervised-repair-labels/error-validated.jsonl
```

Each example here satisfies the strongest condition:

```text
reverse-broken state fails with a structured error
forward-fixed state succeeds
the repair removes at least one diagnostic
```

### Larger validated set

```text
$LATEST/dataset/supervised-repair-labels/validated.jsonl
```

This includes hard errors plus warning/deprecation repairs.

### Warning-only repairs

```text
$LATEST/dataset/supervised-repair-labels/diagnostic-only.jsonl
```

### Failed candidates with explanations

```text
$LATEST/dataset/supervised-repair-labels/rejected.jsonl
```

Do not delete the rejected file. It is how you audit whether the data-selection process is lying to you, a feature many benchmark pipelines apparently regard as optional.

---

## Step 7. View one example

Paste:

```bash
head -n 1 "$LATEST/dataset/supervised-repair-labels/validated.jsonl" | jq '{
  example_id,
  source_kind,
  branch,
  toolchain,
  broken_messages: [.broken_state.diagnostics[].message],
  repaired_files: [.repair.files[].path],
  patch: .repair.patch,
  quality
}'
```

You will see:

- which branch produced the example;
- the exact newer Lean toolchain;
- the errors or warnings in the reversed state;
- which files the repair changed;
- the human-written repair patch;
- validation flags.

---

# Part E. Understand the folders

For one run, the layout is:

```text
~/lean-layer4/runs/TIMESTAMP/
├── empirical/
│   └── adaptation-labels/
│       ├── branches.jsonl
│       ├── labels.jsonl
│       ├── jobs.jsonl
│       ├── skipped.jsonl
│       ├── summary.json
│       └── patches/
├── realized/
│   └── harness/
│       ├── jobs.jsonl
│       ├── runs.jsonl
│       ├── diagnostics.jsonl
│       ├── realizations.jsonl
│       ├── job_failures.jsonl
│       └── summary.json
└── dataset/
    └── supervised-repair-labels/
        ├── candidates.jsonl
        ├── validated.jsonl
        ├── error-validated.jsonl
        ├── diagnostic-only.jsonl
        ├── rejected.jsonl
        └── summary.json
```

Plain-English meaning:

```text
empirical = repair diffs found in Git history
realized  = actual compiler experiments
 dataset  = final joined examples
```

---

# Part F. Run a larger collection

The starter script intentionally makes a small pilot.

To increase it, run the script with environment variables:

```bash
MAX_BRANCHES=30 MAX_LABELS=100 MAX_FILES=50 ./run_layer4_pilot.sh
```

Meaning:

```text
MAX_BRANCHES = number of adaptation branches inspected
MAX_LABELS   = maximum repair labels retained
MAX_FILES    = maximum Lean files compiled per state
```

Increase gradually. Historical Mathlib builds consume real disk, network, memory, and patience. Computers remain stubbornly committed to physics.

Each script invocation creates a new timestamped run directory, so an older run is not overwritten.

---

# Part G. Manual commands, without the script

Use this section only when you need to understand or alter one stage.

## 1. Activate the installed tool

```bash
source ~/lean-layer4/.venv/bin/activate
```

Check it:

```bash
mathlib-repair-harvest layer4 --help
```

## 2. Mine labels

```bash
mkdir -p ~/lean-layer4/manual-run
cd ~/lean-layer4/manual-run

mathlib-repair-harvest layer4 mine \
  --repository ~/lean-layer4/mathlib4-nightly-testing \
  --max-branches 10 \
  --max-labels 10 \
  --max-files 10 \
  --output empirical
```

Inspect:

```bash
jq . empirical/adaptation-labels/summary.json
```

## 3. Execute broken and fixed states

```bash
mathlib-repair-harvest layer4 run \
  --jobs empirical/adaptation-labels/jobs.jsonl \
  --output realized \
  --engine docker \
  --base-image ubuntu:24.04 \
  --file-timeout 900 \
  --setup-timeout 7200
```

Inspect:

```bash
jq . realized/harness/summary.json
```

## 4. Export examples

```bash
mathlib-repair-harvest layer4 export \
  --labels empirical/adaptation-labels/labels.jsonl \
  --harness realized/harness \
  --output dataset
```

Inspect:

```bash
jq . dataset/supervised-repair-labels/summary.json
```

---

# Part H. How to use the JSONL in your taxonomy pipeline

Each line of `error-validated.jsonl` is one JSON object.

Use these fields:

```text
.example_id
    unique example identifier

.source_kind
    "bump" or "lean-pr-testing"

.branch
    source adaptation branch

.toolchain
    exact new Lean toolchain

.broken_state.diagnostics
    structured errors generated by reversing the repair

.repair.patch
    human-written broken-to-fixed target patch

.repair.files
    changed paths and old/new Git blob identifiers

.quality
    validation conditions and diagnostic counts

.leakage_groups
    grouping keys that must stay in one train/validation/test split
```

For a taxonomy classifier, the most useful input is usually:

```text
severity + diagnostic code + diagnostic message + message template
```

For a repair model, the basic supervised pair is:

```text
INPUT  = broken source context + broken_state.diagnostics
TARGET = repair.patch
```

The exported JSONL stores file paths, blob identifiers, the fixed commit, and the patch. To materialize full source text later, reconstruct the fixed commit in a Git worktree and reverse-apply the stored patch, exactly as the harness does.

Never randomly split individual examples before grouping by every value in `leakage_groups`. The same adaptation can appear first in `lean-pr-testing-NNNN` and later in `bump/v4.X.Y`; splitting those copies across train and test would measure memory while wearing a transfer-learning costume.

---

# Part I. Common errors

## `docker: command not found`

Docker Desktop is either not installed or Ubuntu integration is disabled.

Fix:

```text
Docker Desktop → Settings → Resources → WSL Integration → Ubuntu ON → Apply
```

Then close and reopen Ubuntu.

## `Cannot connect to the Docker daemon`

Start Docker Desktop and leave it running. Then test:

```bash
docker info
```

Do not add `sudo` as a ritual sacrifice. Fix the WSL integration instead.

## `mathlib-repair-harvest: command not found`

Activate the virtual environment:

```bash
source ~/lean-layer4/.venv/bin/activate
```

## `label_count` is zero

First update all branches:

```bash
git -C ~/lean-layer4/mathlib4-nightly-testing fetch origin \
  '+refs/heads/*:refs/remotes/origin/*' \
  --tags \
  --prune
```

Then inspect skipped reasons:

```bash
jq . PATH_TO_RUN/empirical/adaptation-labels/skipped.jsonl
```

Try a larger branch sample:

```bash
MAX_BRANCHES=50 MAX_LABELS=20 ./run_layer4_pilot.sh
```

## `bump/v4... has no commits outside master`

That historical bump branch may already have been merged, so its starting boundary is ambiguous. Layer 4 refuses to invent one.

For serious historical mining, supply the true base explicitly:

```bash
mathlib-repair-harvest layer4 mine \
  --repository ~/lean-layer4/mathlib4-nightly-testing \
  --branch-pattern 'bump/v4.30.0' \
  --branch-base 'bump/v4.30.0=THE_REAL_BASE_COMMIT_OR_TAG' \
  --output empirical
```

Do not guess the base just to make the command green. A successfully generated false label remains false.

## The disk is filling up

See Docker's usage:

```bash
docker system df
```

Remove only stopped containers and unused build cache:

```bash
docker container prune
docker builder prune
```

Read each confirmation prompt. Do not blindly run destructive Docker cleanup commands on a machine containing other important projects.

Delete an unwanted timestamped Layer 4 run with:

```bash
rm -rf ~/lean-layer4/runs/THE_UNWANTED_TIMESTAMP
```

Check the path twice before pressing Enter. `rm -rf` does not believe in restorative justice.

---

# Final checklist

Before trusting the data, confirm all of these:

```text
[ ] Ubuntu is WSL 2
[ ] Docker Desktop is running
[ ] Docker WSL integration is enabled for Ubuntu
[ ] the cloned repository is mathlib4-nightly-testing, not ordinary mathlib4
[ ] empirical/adaptation-labels/label_count is above zero
[ ] realized/harness contains structured diagnostics
[ ] dataset/supervised-repair-labels/error-validated.jsonl exists
[ ] rejected.jsonl was inspected
[ ] leakage_groups are respected in dataset splitting
[ ] the pilot was reviewed before scaling
```

The only final training file you should treat as the strongest default is:

```text
error-validated.jsonl
```
