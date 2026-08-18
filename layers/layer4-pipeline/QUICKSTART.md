# QUICKSTART — step by step, assuming nothing

## Step 0 — Open a terminal
- **Mac:** Cmd+Space, type `Terminal`, Enter.
- **Windows:** Start button, type `PowerShell`, Enter.
- **Linux:** you know.

Type one command, press Enter, wait for the prompt to come back, then the next.

## Step 1 — Check your tools
```bash
python3 --version    # want 3.10 or higher (try `python --version` if not found)
git --version        # any version is fine
```
If either is missing, install from python.org / git-scm.com, then come back.

## Step 2 — Make a work folder
```bash
cd ~
mkdir lean-repair
cd lean-repair
```
You are now inside your work folder. Every command below assumes you're here.

## Step 3 — Put this code inside it
Drag this whole `layer4-pipeline` folder into `lean-repair`. Then check:
```bash
ls          # you should see: layer4-pipeline
```

## Step 4 — Install
```bash
cd layer4-pipeline
pip install -e .        # use pip3 if pip is not found
cd ..
python3 -m layer4 --help
```
Seeing a list of commands means it worked.

## Step 5 — Download mathlib AND scan it (one command, takes several minutes)
It will look frozen. It is not. Leave it alone.
```bash
python3 -m layer4 discover --clone-url https://github.com/leanprover-community/mathlib4.git --repo ./mathlib4 -o out/windows.jsonl
```
Ends with `discovered 131 windows`. Check: `ls out` shows `windows.jsonl`.

## Step 6 — Mine the data (start with 5 windows so you can eyeball it)
```bash
python3 -m layer4 mine --repo ./mathlib4 -w out/windows.jsonl -o out --max-windows 5
```
Creates `out/pairs.jsonl` (your data) plus train/val/test splits and a manifest.

## Step 7 — Sanity check
```bash
python3 -m layer4 stats --pairs out/pairs.jsonl   # no label should be over ~40%
head -1 out/pairs.jsonl                            # see one example repair
```

## Step 8 — Full run (drop --max-windows)
```bash
python3 -m layer4 mine --repo ./mathlib4 -w out/windows.jsonl -o out
```

## Step 9 — (the good part) rules + synthetic data
```bash
python3 -m layer4 rules --pairs out/pairs.jsonl -o out/rules.jsonl
python3 -m layer4 synth --repo ./mathlib4 --rules out/rules.jsonl -n 5000 -o out/synthetic.jsonl
```

---

## When something goes wrong
- **`command not found: python3`** → use `python`. **`pip` not found** → use `pip3`.
- **Download seems stuck** → no progress bar; wait 10+ min before worrying.
- **Warning `--split-by toolchain yielded <3 groups`** → harmless; do the full run (Step 8).
- **One label over 40%** → add `--cap-per-label 500` to the mine command.
- **Closed the terminal** → `cd ~/lean-repair` to get back.

Your finished data is `out/pairs.jsonl`. Each line is one labelled repair example.
