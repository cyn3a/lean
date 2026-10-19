#!/usr/bin/env python3
"""LPMA: QLoRA fine-tuning + baseline comparison on a Google Colab T4 GPU.

Trains a LoRA adapter (4-bit QLoRA) on ``lean-errors-finetune.jsonl`` and compares
the fine-tuned model against the same base model *without* fine-tuning on a
held-out test split, following the plan in "Lean Proof Repair Assistant":

* Section 5.2 -- the split is grouped by repository (default) or by version
  transition, never by random shuffling, so near-identical proofs from one repo
  cannot leak between train / validation / test.
* Section 6.1 -- parameter-efficient fine-tuning (LoRA/QLoRA), compared against
  the non-fine-tuned base model.
* Section 8   -- results are also reported per error category.

NOTE: this corpus is the *diagnostic* corpus (Section 5.1): the model predicts the
Lean compiler diagnostic for a migration failure. It contains no verified repairs,
so build success@k cannot be measured here; the comparison uses diagnostic
prediction metrics plus the held-out NLL / perplexity of the gold answers.

Colab usage (Runtime -> Change runtime type -> T4 GPU):

    !pip install -q -U "transformers>=4.46" "peft>=0.13" "accelerate>=1.0" "bitsandbytes>=0.44"
    # upload lean-errors-finetune.jsonl and this script to /content, then:
    !python lpma_colab_finetune_compare.py --data /content/lean-errors-finetune.jsonl

    # quick smoke test (tiny model, few examples, ~5 min):
    !python lpma_colab_finetune_compare.py --model Qwen/Qwen2.5-Coder-0.5B-Instruct --epochs 1 --limit-test 10

To survive Colab disconnects, mount Drive and pass --output-dir /content/drive/MyDrive/lpma_run;
finished stages (baseline predictions, adapter, fine-tuned predictions) are reused on rerun.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import inspect
import json
import math
import os
import random
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

REQUIRED_DIAG_FIELDS = {"file", "line", "column", "message"}


# --------------------------------------------------------------------------- args


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", type=Path, default=Path("/content/lean-errors-finetune.jsonl"))
    p.add_argument("--output-dir", type=Path, default=Path("/content/lpma_run"))
    p.add_argument("--model", default="Qwen/Qwen2.5-Coder-7B-Instruct",
                   help="Any chat model with a system-role chat template. 7B in 4-bit fits a T4.")
    p.add_argument("--split-by", choices=["repo", "transition"], default="repo")
    p.add_argument("--splits-dir", type=Path, default=None,
                   help="Directory written by qc_and_split.py (train/val/test/transition-*.jsonl). When given, "
                        "its QC'd, fork-aware split tags are used instead of re-splitting here; --split-by selects "
                        "the repo-grouped split or the held-out-transition split.")
    # With the current corpus (3 repos hold 224/342 examples) these fractions keep ~10 repos in train.
    p.add_argument("--test-frac", type=float, default=0.15)
    p.add_argument("--val-frac", type=float, default=0.05)
    p.add_argument("--max-group-frac", type=float, default=0.15,
                   help="Groups larger than this fraction of the data are always kept in train.")
    p.add_argument("--seed", type=int, default=13)
    p.add_argument("--max-len", type=int, default=2048, help="Max tokens per training/NLL example.")
    p.add_argument("--epochs", type=float, default=3)
    p.add_argument("--lr", type=float, default=2e-4)
    p.add_argument("--grad-accum", type=int, default=8)
    p.add_argument("--save-steps", type=int, default=100,
                   help="Checkpoint (and evaluate on val) every N optimizer steps; resume reuses the last one.")
    p.add_argument("--lora-r", type=int, default=16)
    p.add_argument("--lora-alpha", type=int, default=32)
    p.add_argument("--lora-dropout", type=float, default=0.05)
    p.add_argument("--max-new-tokens", type=int, default=512)
    p.add_argument("--eval-batch-size", type=int, default=2,
                   help="Generation batch size; halved automatically on CUDA OOM.")
    p.add_argument("--limit-test", type=int, default=0, help="Evaluate only the first N test examples (0 = all).")
    p.add_argument("--bootstrap", type=int, default=2000)
    p.add_argument("--skip-baseline", action="store_true")
    # parse_known_args tolerates the extra "-f kernel.json" argument when run via %run in Colab.
    args, _ = p.parse_known_args()
    return args


# --------------------------------------------------------------------------- data


def load_dataset(path: Path) -> list[dict]:
    if not path.exists():
        try:
            from google.colab import files  # type: ignore

            print(f"{path} not found -- please upload lean-errors-finetune.jsonl")
            uploaded = files.upload()
            name = next(iter(uploaded))
            path = Path("/content") / name
        except ImportError:
            raise SystemExit(f"Dataset not found: {path}")
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            msgs = row["messages"]
            roles = [m["role"] for m in msgs]
            if roles != ["system", "user", "assistant"]:
                raise ValueError(f"line {line_no}: unexpected roles {roles}")
            request = json.loads(msgs[1]["content"])
            gold = json.loads(msgs[2]["content"])["diagnostics"]
            rows.append({
                "idx": len(rows),
                "split_tag": row.get("split"),
                "messages": msgs,
                "repo": request["repo"],
                "transition": f'{request["old_version"]} -> {request["new_version"]}',
                "gold": gold,
            })
    print(f"Loaded {len(rows)} examples from {path}")
    return rows


def load_presplit(splits_dir: Path, split_by: str):
    """Use the Section 5.2 splits produced by qc_and_split.py (fork/duplicate-aware groups).

    repo:       train/val/test = the repo-grouped split.
    transition: test = the held-out version transitions; train = the remaining examples, except
                those that the repo split marks as validation, which serve as val here.
    Every row carries ``split = {group, repo_split, transition, transition_split}``.
    """
    rows = []
    for name in ("train", "val", "test"):
        path = splits_dir / f"{name}.jsonl"
        if path.exists():
            rows.extend(load_dataset(path))
    for i, r in enumerate(rows):
        r["idx"] = i
    splits = {"train": [], "val": [], "test": []}
    assignment = {}
    for r in rows:
        tag = r["split_tag"]
        if split_by == "repo":
            dest, key = tag["repo_split"], tag["group"]
        else:
            key = tag["transition"]
            if tag["transition_split"] == "test":
                dest = "test"
            elif tag["repo_split"] == "val":
                dest = "val"
            else:
                dest = "train"
        splits[dest].append(r)
        assignment[key] = dest if split_by == "repo" or dest != "val" else "train"
    for name, items in splits.items():
        n_groups = len({r["split_tag"]["group" if split_by == "repo" else "transition"] for r in items})
        print(f"  {name:5s}: {len(items):4d} examples from {n_groups} {split_by} groups")
    if not splits["test"] or not splits["train"]:
        raise SystemExit("Pre-split data has an empty train or test set.")
    return splits, assignment


def grouped_split(rows, key, test_frac, val_frac, max_group_frac, seed):
    """Assign whole groups (repos or transitions) to test, then val; the rest is train."""
    sizes = Counter(r[key] for r in rows)
    total = len(rows)
    groups = sorted(sizes)
    random.Random(seed).shuffle(groups)
    big = {g for g in groups if sizes[g] > max_group_frac * total}
    assignment = {g: "train" for g in big}
    targets = [("test", test_frac * total), ("val", val_frac * total)]
    remaining = [g for g in groups if g not in big]
    for split, target in targets:
        filled = 0
        for g in list(remaining):
            if filled >= target:
                break
            # Do not overshoot the target by more than half of it.
            if filled and filled + sizes[g] > 1.5 * target:
                continue
            assignment[g] = split
            filled += sizes[g]
            remaining.remove(g)
    for g in remaining:
        assignment[g] = "train"
    splits = {"train": [], "val": [], "test": []}
    for r in rows:
        splits[assignment[r[key]]].append(r)
    for name, items in splits.items():
        n_groups = len({r[key] for r in items})
        print(f"  {name:5s}: {len(items):4d} examples from {n_groups} {key} groups")
    if not splits["test"] or not splits["train"]:
        raise SystemExit("Split produced an empty train or test set; adjust --test-frac/--max-group-frac.")
    return splits, assignment


# --------------------------------------------------------------------------- metrics


_META_RE = [
    (re.compile(r"\?(m|u)\.\d+"), r"?\1"),
    (re.compile(r"_uniq\.\d+"), "_uniq"),
    (re.compile(r"✝\d*"), "✝"),
    (re.compile(r"\s+"), " "),
]


def normalize_message(msg: str) -> str:
    msg = msg.strip()
    for pattern, repl in _META_RE:
        msg = pattern.sub(repl, msg)
    return msg


def headline(msg: str) -> str:
    first = msg.strip().split("\n", 1)[0]
    return normalize_message(first)


def error_kind(msg: str) -> str:
    """Coarse diagnostic category: the headline up to the first quoted identifier / colon."""
    head = headline(msg)
    head = re.split(r"[`'‘’:\"]", head, maxsplit=1)[0]
    head = re.sub(r"\d+", "N", head).strip().lower()
    return head[:60] or "(empty)"


def extract_json(text: str):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    # Accept a bare array too: untuned models often drop the {"diagnostics": ...} wrapper.
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        return None
    start = min(starts)
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[start:])
        return obj
    except json.JSONDecodeError:
        return None


def schema_ok(obj) -> bool:
    if not isinstance(obj, dict) or set(obj) != {"diagnostics"} or not isinstance(obj["diagnostics"], list):
        return False
    for d in obj["diagnostics"]:
        if not isinstance(d, dict) or set(d) != REQUIRED_DIAG_FIELDS:
            return False
        if not isinstance(d["file"], str) or not isinstance(d["message"], str):
            return False
        if type(d["line"]) is not int or type(d["column"]) is not int:
            return False
    return True


def similarity(a: str, b: str, cap: int = 3000) -> float:
    return difflib.SequenceMatcher(None, a[:cap], b[:cap], autojunk=False).ratio()


def score_example(prediction: str, gold: list[dict]) -> dict:
    obj = extract_json(prediction)
    valid = obj is not None
    schema = valid and schema_ok(obj)
    # Content metrics are lenient about the wrapper; schema_valid alone checks the exact format.
    pred = []
    if isinstance(obj, dict) and isinstance(obj.get("diagnostics"), list):
        pred = [d for d in obj["diagnostics"] if isinstance(d, dict)]
    elif isinstance(obj, list):
        pred = [d for d in obj if isinstance(d, dict)]
    elif isinstance(obj, dict) and "message" in obj:
        pred = [obj]
    pred_locs = {(d.get("file"), d.get("line"), d.get("column")) for d in pred}
    pred_lines = {(d.get("file"), d.get("line")) for d in pred}
    pred_msgs = [normalize_message(str(d.get("message", ""))) for d in pred]
    pred_heads = {headline(str(d.get("message", ""))) for d in pred}

    n = len(gold)
    loc = line = exact = head = sim = 0.0
    for g in gold:
        gm = normalize_message(g["message"])
        loc += (g["file"], g["line"], g["column"]) in pred_locs
        line += (g["file"], g["line"]) in pred_lines
        exact += gm in pred_msgs
        head += headline(g["message"]) in pred_heads
        sim += max((similarity(gm, pm) for pm in pred_msgs), default=0.0)
    gold_canon = sorted((g["file"], g["line"], g["column"], normalize_message(g["message"])) for g in gold)
    pred_canon = sorted((d.get("file"), d.get("line"), d.get("column"),
                         normalize_message(str(d.get("message", "")))) for d in pred)
    return {
        "json_valid": float(valid),
        "schema_valid": float(schema),
        "count_match": float(len(pred) == n),
        "location_exact": loc / n,
        "line_match": line / n,
        "headline_match": head / n,
        "message_exact": exact / n,
        "message_similarity": sim / n,
        "answer_exact": float(schema and gold_canon == pred_canon),
    }


METRICS = ["json_valid", "schema_valid", "count_match", "location_exact", "line_match",
           "headline_match", "message_exact", "message_similarity", "answer_exact"]


def paired_bootstrap(base, ft, n_boot, seed):
    import numpy as np

    base = np.asarray(base, dtype=float)
    ft = np.asarray(ft, dtype=float)
    diff = ft - base
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(diff), size=(n_boot, len(diff)))
    boots = diff[idx].mean(axis=1)
    lo, hi = np.percentile(boots, [2.5, 97.5])
    p_le_zero = float((boots <= 0).mean())
    return float(diff.mean()), float(lo), float(hi), p_le_zero


# --------------------------------------------------------------------------- model helpers


def tokenize_example(tok, messages, max_len):
    prompt = tok.apply_chat_template(messages[:2], tokenize=False, add_generation_prompt=True)
    full = tok.apply_chat_template(messages, tokenize=False)
    if not full.startswith(prompt):
        full = prompt + messages[2]["content"] + (tok.eos_token or "")
    p_ids = tok(prompt, add_special_tokens=False)["input_ids"]
    a_ids = tok(full[len(prompt):], add_special_tokens=False)["input_ids"]
    if len(p_ids) >= max_len - 32:
        return None  # prompt alone does not fit; nothing to learn from
    ids = (p_ids + a_ids)[:max_len]
    labels = ([-100] * len(p_ids) + a_ids)[:max_len]
    return {"input_ids": ids, "attention_mask": [1] * len(ids), "labels": labels}


class Collator:
    def __init__(self, pad_id):
        self.pad_id = pad_id

    def __call__(self, batch):
        import torch

        width = max(len(b["input_ids"]) for b in batch)
        out = {"input_ids": [], "attention_mask": [], "labels": []}
        for b in batch:
            pad = width - len(b["input_ids"])
            out["input_ids"].append(b["input_ids"] + [self.pad_id] * pad)
            out["attention_mask"].append(b["attention_mask"] + [0] * pad)
            out["labels"].append(b["labels"] + [-100] * pad)
        return {k: torch.tensor(v) for k, v in out.items()}


def load_base_model(name):
    import torch
    import transformers
    from packaging import version
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

    if not torch.cuda.is_available():
        raise SystemExit("No CUDA GPU found. In Colab: Runtime -> Change runtime type -> T4 GPU.")
    print(f"GPU: {torch.cuda.get_device_name(0)}  |  transformers {transformers.__version__}")
    tok = AutoTokenizer.from_pretrained(name)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    bnb = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_use_double_quant=True,
        bnb_4bit_compute_dtype=torch.float16,  # T4 has no bf16
    )
    dtype_kw = "dtype" if version.parse(transformers.__version__) >= version.parse("4.56") else "torch_dtype"
    model = AutoModelForCausalLM.from_pretrained(
        name,
        quantization_config=bnb,
        device_map={"": 0},
        attn_implementation="sdpa",
        **{dtype_kw: torch.float16},
    )
    model.config.use_cache = True
    return model, tok


def generate_predictions(model, tok, rows, max_new_tokens, batch_size, cache_path: Path, label: str):
    import torch

    if cache_path.exists():
        cached = [json.loads(l) for l in cache_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        if {c["idx"] for c in cached} == {r["idx"] for r in rows}:
            print(f"[{label}] reusing cached predictions from {cache_path}")
            return {c["idx"]: c for c in cached}
    model.eval()
    model.config.use_cache = True
    tok.padding_side = "left"
    prompts = {r["idx"]: tok.apply_chat_template(r["messages"][:2], tokenize=False, add_generation_prompt=True)
               for r in rows}
    order = sorted(rows, key=lambda r: len(prompts[r["idx"]]))
    results = {}
    t0 = time.time()
    def run_batch(batch):
        """Generate for one batch; on CUDA OOM split the batch, and at size 1 record the failure."""
        enc = tok([prompts[r["idx"]] for r in batch], return_tensors="pt", padding=True,
                  add_special_tokens=False).to(model.device)
        try:
            with torch.no_grad():
                out = model.generate(
                    **enc,
                    max_new_tokens=max_new_tokens,
                    do_sample=False,
                    temperature=None, top_p=None, top_k=None,
                    pad_token_id=tok.pad_token_id,
                )
        except torch.cuda.OutOfMemoryError:
            del enc
            torch.cuda.empty_cache()
            if len(batch) == 1:
                r = batch[0]
                print(f"[{label}] CUDA OOM on example {r['idx']} even at batch size 1 "
                      f"({len(prompts[r['idx']])} prompt chars); recorded as an empty prediction", flush=True)
                results[r["idx"]] = {"idx": r["idx"], "prediction": "", "truncated": False, "oom": True}
                return
            half = len(batch) // 2
            print(f"[{label}] CUDA OOM at batch size {len(batch)}; retrying as {half} + {len(batch) - half}", flush=True)
            run_batch(batch[:half])
            run_batch(batch[half:])
            return
        new_tokens = out[:, enc["input_ids"].shape[1]:]
        for r, seq in zip(batch, new_tokens):
            text = tok.decode(seq, skip_special_tokens=True)
            results[r["idx"]] = {"idx": r["idx"], "prediction": text,
                                 "truncated": bool((seq != tok.pad_token_id).sum() >= max_new_tokens)}
        del enc, out

    for start in range(0, len(order), batch_size):
        run_batch(order[start:start + batch_size])
        done = min(start + batch_size, len(order))
        print(f"[{label}] generated {done}/{len(order)}  ({time.time() - t0:.0f}s)", flush=True)
    n_oom = sum(1 for v in results.values() if v.get("oom"))
    if n_oom:
        print(f"[{label}] {n_oom} example(s) could not be generated because of CUDA OOM; "
              f"they count as failed predictions in the metrics", flush=True)
    cache_path.write_text("\n".join(json.dumps(results[r["idx"]], ensure_ascii=False) for r in rows) + "\n",
                          encoding="utf-8")
    return results


def answer_nll(model, tok, rows, max_len, label):
    """Per-example mean NLL of the gold answer tokens (teacher forcing)."""
    import torch

    model.eval()
    collate = Collator(tok.pad_token_id)
    out = {}
    for r in rows:
        feats = tokenize_example(tok, r["messages"], max_len)
        if feats is None:
            continue
        n_tokens = sum(1 for x in feats["labels"] if x != -100)
        if n_tokens == 0:
            continue
        batch = {k: v.to(model.device) for k, v in collate([feats]).items()}
        try:
            with torch.no_grad():
                loss = model(**batch).loss.item()
        except torch.cuda.OutOfMemoryError:
            del batch
            torch.cuda.empty_cache()
            print(f"[{label}] CUDA OOM scoring example {r['idx']} ({len(feats['input_ids'])} tokens); skipped", flush=True)
            continue
        out[r["idx"]] = (loss, n_tokens)
        del batch
    total = sum(l * n for l, n in out.values())
    count = sum(n for _, n in out.values())
    print(f"[{label}] gold-answer NLL {total / count:.4f}  perplexity {math.exp(total / count):.2f}")
    return out


# --------------------------------------------------------------------------- training


def make_answer_logits_trainer():
    """Trainer subclass whose LM loss touches only the answer positions.

    The stock causal-LM forward materializes fp32 logits for every position; for Qwen2.5's
    152k-token vocabulary that is 1.2 GB per 2048-token sequence, and the backward pass needs
    two or three such tensors.  That is what ran a 7B QLoRA step out of memory on a 15 GB T4
    (``Tried to allocate 1.16 GiB``).  Prompts here are long and answers short, so the model is
    asked for logits only at the positions that predict answer tokens (``logits_to_keep``
    accepts an index tensor since transformers 4.45) and cross-entropy is computed there.
    If the model ignores the index tensor, the full logits it returned are used instead.
    """
    from transformers import Trainer

    class AnswerLogitsTrainer(Trainer):
        def compute_loss(self, model, inputs, return_outputs=False, num_items_in_batch=None):
            import torch
            import torch.nn.functional as F

            labels = inputs.get("labels")
            if labels is None or labels.shape[0] != 1:
                return super().compute_loss(model, inputs, return_outputs=return_outputs,
                                            num_items_in_batch=num_items_in_batch)
            shifted = labels[0, 1:]
            positions = torch.nonzero(shifted != -100, as_tuple=False).squeeze(-1)
            model_inputs = {k: v for k, v in inputs.items() if k != "labels"}
            try:
                outputs = model(**model_inputs, logits_to_keep=positions)
            except TypeError:
                outputs = model(**model_inputs)
            logits = outputs.logits[0]
            if logits.shape[0] == positions.numel() and positions.numel() > 0:
                targets = shifted[positions]
            else:  # index tensor not honoured: full logits came back
                logits, targets = logits[:-1], shifted
            loss_sum = F.cross_entropy(logits.float(), targets, ignore_index=-100, reduction="sum")
            n_targets = int((targets != -100).sum().item())
            if num_items_in_batch is not None:
                loss = loss_sum / num_items_in_batch
            else:
                loss = loss_sum / max(n_targets, 1)
            return (loss, outputs) if return_outputs else loss

    return AnswerLogitsTrainer


def train_adapter(model, tok, splits, args, adapter_dir: Path):
    import torch
    from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
    from transformers import TrainingArguments

    Trainer = make_answer_logits_trainer()

    model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)
    lora = LoraConfig(
        r=args.lora_r,
        lora_alpha=args.lora_alpha,
        lora_dropout=args.lora_dropout,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
    )
    model = get_peft_model(model, lora)
    model.print_trainable_parameters()

    def featurize(rows, name):
        feats = [f for f in (tokenize_example(tok, r["messages"], args.max_len) for r in rows) if f]
        clipped = sum(1 for f in feats if len(f["input_ids"]) == args.max_len)
        print(f"  {name}: {len(feats)} features ({len(rows) - len(feats)} skipped, {clipped} clipped at max-len)")
        return feats

    train_feats = featurize(splits["train"], "train")
    val_feats = featurize(splits["val"], "val") if splits["val"] else None

    # transformers 5.x dropped warmup_ratio; its warmup_steps accepts a float ratio instead.
    accepted = inspect.signature(TrainingArguments.__init__).parameters
    warmup = {"warmup_ratio": 0.05} if "warmup_ratio" in accepted else {"warmup_steps": 0.05}
    targs = TrainingArguments(
        output_dir=str(adapter_dir.parent / "checkpoints"),
        num_train_epochs=args.epochs,
        per_device_train_batch_size=1,
        per_device_eval_batch_size=1,
        gradient_accumulation_steps=args.grad_accum,
        learning_rate=args.lr,
        lr_scheduler_type="cosine",
        **warmup,
        weight_decay=0.0,
        max_grad_norm=0.3,
        fp16=True,
        bf16=False,
        optim="paged_adamw_8bit",
        gradient_checkpointing=True,
        gradient_checkpointing_kwargs={"use_reentrant": False},
        logging_steps=5,
        # Step-based checkpoints so a Colab disconnect loses at most ~save_steps optimizer steps.
        eval_strategy="steps" if val_feats else "no",
        eval_steps=args.save_steps,
        save_strategy="steps",
        save_steps=args.save_steps,
        save_total_limit=1,
        load_best_model_at_end=bool(val_feats),
        metric_for_best_model="eval_loss",
        greater_is_better=False,
        remove_unused_columns=False,
        report_to="none",
        seed=args.seed,
    )
    model.config.use_cache = False
    trainer = Trainer(
        model=model,
        args=targs,
        train_dataset=train_feats,
        eval_dataset=val_feats,
        data_collator=Collator(tok.pad_token_id),
    )
    # Resume from the last epoch checkpoint if a previous runtime died mid-training.
    from transformers.trainer_utils import get_last_checkpoint

    ckpt_dir = Path(targs.output_dir)
    last = get_last_checkpoint(str(ckpt_dir)) if ckpt_dir.is_dir() else None
    if last:
        print(f"Resuming training from {last}")
    t0 = time.time()
    trainer.train(resume_from_checkpoint=last)
    print(f"Training finished in {(time.time() - t0) / 60:.1f} min")
    history = trainer.state.log_history
    model = trainer.model
    model.save_pretrained(str(adapter_dir))
    tok.save_pretrained(str(adapter_dir))
    (adapter_dir / "log_history.json").write_text(json.dumps(history, indent=2), encoding="utf-8")
    del trainer
    torch.cuda.empty_cache()
    return model


# --------------------------------------------------------------------------- report


def build_report(test_rows, preds, nll, args, out: Path, split_info: dict):
    systems = [s for s in ("baseline", "finetuned") if s in preds]
    per_example = []
    for r in test_rows:
        row = {"idx": r["idx"], "repo": r["repo"], "transition": r["transition"],
               "error_kind": error_kind(r["gold"][0]["message"])}
        for s in systems:
            scores = score_example(preds[s][r["idx"]]["prediction"], r["gold"])
            for m, v in scores.items():
                row[f"{s}.{m}"] = v
            row[f"{s}.truncated"] = float(preds[s][r["idx"]]["truncated"])
            if r["idx"] in nll.get(s, {}):
                row[f"{s}.nll"] = nll[s][r["idx"]][0]
        per_example.append(row)

    with (out / "per_example.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(per_example[0]))
        writer.writeheader()
        writer.writerows(per_example)

    summary = {"model": args.model, "n_test": len(test_rows), "split": split_info, "metrics": {}}
    lines = [
        "# LPMA fine-tuning vs. baseline",
        "",
        f"- Base model: `{args.model}` (4-bit QLoRA, r={args.lora_r}, {args.epochs} epochs, lr={args.lr})",
        f"- Split by **{args.split_by}**: " + ", ".join(f"{k}={v}" for k, v in split_info["sizes"].items()),
        f"- Test {args.split_by} groups: " + ", ".join(f"`{g}`" for g in split_info["test_groups"]),
        f"- Task: predict the Lean diagnostic JSON (diagnostic corpus; build success@k needs the repair corpus).",
        "",
        "| Metric | Baseline | Fine-tuned | Δ (95% bootstrap CI) | P(Δ≤0) |",
        "|---|---:|---:|---:|---:|",
    ]
    for m in METRICS + ["truncated"]:
        cols = {s: [row[f"{s}.{m}"] for row in per_example] for s in systems}
        entry = {s: sum(v) / len(v) for s, v in cols.items()}
        cells = [f"{entry[s]:.3f}" if s in entry else "–" for s in ("baseline", "finetuned")]
        delta = "–"
        p = "–"
        if len(systems) == 2 and m != "truncated":
            d, lo, hi, p_le = paired_bootstrap(cols["baseline"], cols["finetuned"], args.bootstrap, args.seed)
            entry.update({"delta": d, "ci95": [lo, hi], "p_delta_le_0": p_le})
            delta = f"{d:+.3f} [{lo:+.3f}, {hi:+.3f}]"
            p = f"{p_le:.3f}"
        summary["metrics"][m] = entry
        lines.append(f"| {m} | {cells[0]} | {cells[1]} | {delta} | {p} |")

    for s in systems:
        if s in nll and nll[s]:
            shared = [i for i in nll[s] if all(i in nll.get(t, {}) for t in systems)]
            total = sum(nll[s][i][0] * nll[s][i][1] for i in shared)
            count = sum(nll[s][i][1] for i in shared)
            summary["metrics"].setdefault("gold_answer_nll", {})[s] = total / count
            summary["metrics"].setdefault("gold_answer_perplexity", {})[s] = math.exp(total / count)
    if "gold_answer_nll" in summary["metrics"]:
        g = summary["metrics"]["gold_answer_nll"]
        ppl = summary["metrics"]["gold_answer_perplexity"]
        lines.append(
            "| gold-answer NLL ↓ | " + " | ".join(f"{g[s]:.3f}" if s in g else "–" for s in ("baseline", "finetuned"))
            + " | | |")
        lines.append(
            "| gold-answer perplexity ↓ | "
            + " | ".join(f"{ppl[s]:.2f}" if s in ppl else "–" for s in ("baseline", "finetuned")) + " | | |")

    # Per error-category breakdown (Section 8 of the paper).
    by_kind = defaultdict(list)
    for row in per_example:
        by_kind[row["error_kind"]].append(row)
    lines += ["", "## By error category (test examples with n ≥ 3)", "",
              "| Error kind | n | " + " | ".join(f"{s} headline / similarity" for s in systems) + " |",
              "|---|---:|" + "---:|" * len(systems)]
    summary["by_error_kind"] = {}
    for kind, items in sorted(by_kind.items(), key=lambda kv: -len(kv[1])):
        if len(items) < 3:
            continue
        cells, entry = [], {"n": len(items)}
        for s in systems:
            h = sum(i[f"{s}.headline_match"] for i in items) / len(items)
            sim = sum(i[f"{s}.message_similarity"] for i in items) / len(items)
            cells.append(f"{h:.2f} / {sim:.2f}")
            entry[s] = {"headline_match": h, "message_similarity": sim}
        summary["by_error_kind"][kind] = entry
        lines.append(f"| {kind} | {len(items)} | " + " | ".join(cells) + " |")

    # Side-by-side examples where fine-tuning helped / hurt the most.
    if len(systems) == 2:
        ranked = sorted(per_example, key=lambda r: r["finetuned.message_similarity"] - r["baseline.message_similarity"])
        picks = [("Largest improvements", ranked[::-1][:4]), ("Largest regressions", ranked[:3])]
        by_idx = {r["idx"]: r for r in test_rows}
        examples = ["# Side-by-side examples", ""]
        for title, items in picks:
            examples += [f"## {title}", ""]
            for row in items:
                r = by_idx[row["idx"]]
                examples += [
                    f"### #{r['idx']} `{r['repo']}` ({r['transition']})",
                    f"similarity baseline {row['baseline.message_similarity']:.2f} → "
                    f"fine-tuned {row['finetuned.message_similarity']:.2f}",
                    "", "**Gold**", "```json", r["messages"][2]["content"][:2000], "```",
                    "**Baseline**", "```", preds["baseline"][r["idx"]]["prediction"][:2000], "```",
                    "**Fine-tuned**", "```", preds["finetuned"][r["idx"]]["prediction"][:2000], "```", "",
                ]
        (out / "examples.md").write_text("\n".join(examples), encoding="utf-8")

    (out / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    report = "\n".join(lines) + "\n"
    (out / "comparison.md").write_text(report, encoding="utf-8")
    print("\n" + report)
    print(f"Wrote {out / 'comparison.md'}, summary.json, per_example.csv, examples.md")


# --------------------------------------------------------------------------- main


def main() -> int:
    args = parse_args()
    random.seed(args.seed)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    if args.splits_dir:
        print(f"Using pre-split data from {args.splits_dir} ({args.split_by} split):")
        splits, assignment = load_presplit(args.splits_dir, args.split_by)
    else:
        rows = load_dataset(args.data)
        print(f"Grouped split by {args.split_by}:")
        splits, assignment = grouped_split(rows, args.split_by, args.test_frac, args.val_frac,
                                           args.max_group_frac, args.seed)
    test_rows = splits["test"][: args.limit_test] if args.limit_test else splits["test"]
    split_info = {
        "by": args.split_by,
        "source": str(args.splits_dir) if args.splits_dir else "grouped_split",
        "sizes": {k: len(v) for k, v in splits.items()},
        "test_groups": sorted(g for g, s in assignment.items() if s == "test"),
        "val_groups": sorted(g for g, s in assignment.items() if s == "val"),
    }
    (args.output_dir / "split.json").write_text(json.dumps(split_info, indent=2), encoding="utf-8")

    # Reduce fragmentation-driven OOMs on the 15 GB T4 (must be set before CUDA initializes).
    os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
    import torch
    from transformers import set_seed

    set_seed(args.seed)
    model, tok = load_base_model(args.model)
    preds, nll = {}, {}

    # 1) Baseline: the same base model, no fine-tuning, same prompts and decoding.
    if not args.skip_baseline:
        preds["baseline"] = generate_predictions(model, tok, test_rows, args.max_new_tokens, args.eval_batch_size,
                                                 args.output_dir / "baseline_predictions.jsonl", "baseline")
        nll["baseline"] = answer_nll(model, tok, test_rows, args.max_len, "baseline")

    # 2) QLoRA fine-tuning (or reuse a saved adapter).
    adapter_dir = args.output_dir / "adapter"
    if (adapter_dir / "adapter_config.json").exists():
        from peft import PeftModel

        print(f"Reusing trained adapter from {adapter_dir}")
        model = PeftModel.from_pretrained(model, str(adapter_dir))
    else:
        model = train_adapter(model, tok, splits, args, adapter_dir)

    # 3) Fine-tuned evaluation.
    model.config.use_cache = True
    preds["finetuned"] = generate_predictions(model, tok, test_rows, args.max_new_tokens, args.eval_batch_size,
                                              args.output_dir / "finetuned_predictions.jsonl", "finetuned")
    nll["finetuned"] = answer_nll(model, tok, test_rows, args.max_len, "finetuned")

    build_report(test_rows, preds, nll, args, args.output_dir, split_info)
    torch.cuda.empty_cache()
    return 0


if __name__ == "__main__":
    sys.exit(main())
