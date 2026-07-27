# Layer 3 quick start

This is the easiest way to run the Layer 3 pilot on Windows.

The pilot automatically:

1. clones Lean 4 and Mathlib;
2. reads Lean’s versioned expected diagnostics;
3. reads one Reservoir package;
4. creates the Mathlib old/broken/fixed experiments;
5. runs them in one Docker container per Lean toolchain;
6. calculates empirical frequency weights.

You will type a few setup commands, then run one script. A rare triumph over unnecessary ceremony.

---

## Step 1: install WSL and Ubuntu

Open **PowerShell as Administrator** and run:

```powershell
wsl --install
```

Restart Windows if requested. Then open the **Ubuntu** application.

Already using Ubuntu, Linux, or macOS? Skip this step.

---

## Step 2: install Docker Desktop

Install Docker Desktop.

In Docker Desktop, open:

```text
Settings → Resources → WSL Integration
```

Enable Ubuntu.

Open Ubuntu and test Docker:

```bash
docker run --rm ubuntu:24.04 echo "Docker works"
```

Do not continue until the output says:

```text
Docker works
```

---

## Step 3: install the small Ubuntu requirements

In Ubuntu, run:

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv git jq curl
```

---

## Step 4: download and install Layer 3

Download these two files from the chat:

```text
mathlib_repair_harvester-0.2.0-py3-none-any.whl
run_layer3_pilot.sh
```

Create an Ubuntu folder and open it in Windows Explorer:

```bash
mkdir -p ~/layer3-install
cd ~/layer3-install
explorer.exe .
```

Drag both downloaded files into the Explorer window.

Back in Ubuntu, install the wheel:

```bash
cd ~/layer3-install
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install ./mathlib_repair_harvester-0.2.0-py3-none-any.whl
```

Check it:

```bash
mathlib-repair-harvest layer3 --help
```

You should see:

```text
core-tests
reservoir
mathlib-history
run
weights
```

---

## Step 5: run the complete pilot

Make sure Docker Desktop is open. Then run:

```bash
cd ~/layer3-install
source .venv/bin/activate
bash run_layer3_pilot.sh
```

The script prints eight numbered stages:

```text
[1/8] Cloning Lean 4
[2/8] Cloning Mathlib
[3/8] Reading Lean core expected diagnostics
[4/8] Reading one Reservoir package
[5/8] Planning the Mathlib three-state experiment
[6/8] Creating a small pilot job list
[7/8] Running structured jobs in Docker
[8/8] Computing empirical frequency weights
```

Do not close Docker Desktop or the Ubuntu terminal during the script.

---

## Step 6: check the result

Run:

```bash
jq . ~/lean-layer3/empirical/frequency-weights/summary.json
```

Then check that the four main outputs exist:

```bash
ls -l \
  ~/lean-layer3/empirical/frequency-weights/signature_weights.jsonl \
  ~/lean-layer3/empirical/frequency-weights/template_weights.jsonl \
  ~/lean-layer3/empirical/frequency-weights/totals.json \
  ~/lean-layer3/empirical/frequency-weights/summary.json
```

### What the files mean

```text
signature_weights.jsonl
```

Exact diagnostic frequencies, including severity, code, and exact normalized message.

```text
template_weights.jsonl
```

Broader diagnostic families, with changing identifiers, paths, numbers, and hashes normalized away.

```text
totals.json
```

Corpus totals used in the calculations.

```text
summary.json
```

A small summary of the completed weighting run.

The most important field in either weights file is:

```text
frequency_weight
```

Higher means the diagnostic is more common after balancing the different corpora.

---

# Opening the results in Windows

Run:

```bash
cd ~/lean-layer3/empirical/frequency-weights
explorer.exe .
```

Windows Explorer will open the result folder.

---

# Running it again later

Open Ubuntu and run:

```bash
cd ~/layer3-install
source .venv/bin/activate
bash run_layer3_pilot.sh
```

The repositories are reused instead of cloned again.

---

# Three common fixes

## `mathlib-repair-harvest: command not found`

Activate the environment:

```bash
source ~/layer3-install/.venv/bin/activate
```

## Docker error

Open Docker Desktop and test:

```bash
docker run --rm ubuntu:24.04 echo OK
```

Also confirm that Ubuntu is enabled under Docker Desktop’s WSL Integration settings.

## The script says that no jobs were generated

Run this replacement Mathlib command:

```bash
source ~/layer3-install/.venv/bin/activate

mathlib-repair-harvest layer3 mathlib-history \
  --repository "$HOME/lean-layer3/repos/mathlib4" \
  --tag-pattern 'v4.*' \
  --max-refs 2 \
  --max-files 5 \
  --backend lean-json \
  --include-same-toolchain \
  --output "$HOME/lean-layer3/empirical"
```

Then rerun:

```bash
bash ~/layer3-install/run_layer3_pilot.sh
```

---

# Making the experiment larger

The pilot deliberately uses only:

```text
2 release refs
5 Mathlib files
1 Reservoir package
```

After it works, edit `run_layer3_pilot.sh` and change:

```text
--max-refs 2
--max-files 5
```

to, for example:

```text
--max-refs 5
--max-files 50
```

Keep the first run small. “Let us immediately compile everything” is not a methodology, merely an expensive mood.
