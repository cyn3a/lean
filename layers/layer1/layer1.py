import json
import re
import os
import time
import subprocess
from pathlib import Path

################################################################################
# CONFIG
################################################################################

REPO = r"C:\dev\lean-full\lean4"

# Progress is APPENDED here, one JSON object per line (JSON Lines).
# Appending never rewrites the existing file, so it can't be locked out
# mid-run and can't be corrupted by a crash.
PROGRESS_FILE = Path("error_targets_search.jsonl")

# Optional: once every tag is done, fold the .jsonl into a single JSON object
# of the same shape the old script produced ({tag: [hits...]}).
OUTPUT_FILE = Path("error_targets_search.json")
WRITE_COMBINED_JSON = True

# If True, tags already recorded in PROGRESS_FILE are skipped on re-run.
# Set to False to wipe progress and re-scan everything.
RESUME = True

TARGETS = [
    "throwError",
    "throwErrorAt",
    "logError",
    "logWarning",
    "logInfo",
    "throwUnsupportedSyntax",
    "throwTacticEx",
    "throwNestedTacticEx",
    "throwAppTypeMismatch",
    "throwFunctionExpected",
    "throwTypeMismatchError",
    "throwIllFormedSyntax",
    "throwKernelException",
    "throwUnknownConstant",
    "throwAbortCommand",
    "panic!",
    "unreachable!",
]

# Lean identifiers may contain letters, digits, _, ', ! and ?.
# Bounding on those characters stops "throwError" from also matching
# "throwErrorAt", while still allowing a leading "." (field notation).
_IDENT = r"A-Za-z0-9_'!?"

TARGET_REGEXES = {
    target: re.compile(rf"(?<![{_IDENT}]){re.escape(target)}(?![{_IDENT}])")
    for target in TARGETS
}


################################################################################
# PROGRESS PERSISTENCE
################################################################################

def _open_with_retry(path, mode, attempts=10, delay=0.5):
    """
    On Windows, antivirus / Search Indexer / an open editor can hold a brief
    lock on a file that was just written, which surfaces as PermissionError.
    Retrying with a short backoff clears it in practice.
    """
    last_err = None
    for i in range(attempts):
        try:
            return open(path, mode, encoding="utf-8")
        except PermissionError as e:
            last_err = e
            if i == 0:
                print(f"  {path.name} is locked, retrying...")
            time.sleep(delay * (i + 1))
    raise last_err


def _replace_with_retry(src, dst, attempts=10, delay=0.5):
    """os.replace() with the same Windows lock tolerance."""
    last_err = None
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError as e:
            last_err = e
            time.sleep(delay * (i + 1))
    raise last_err


def load_done_tags(path):
    """
    Return the set of tags already recorded.

    Only the tag names are kept in memory, not the hits -- with 135 tags x
    ~3700 hits, holding everything would cost hundreds of MB for no reason.
    A partially written final line (from a crash mid-append) is ignored.
    """
    done = set()
    if not path.exists():
        return done

    bad_lines = 0
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                bad_lines += 1
                continue
            tag = record.get("tag")
            if tag:
                done.add(tag)

    if bad_lines:
        print(f"Ignored {bad_lines} incomplete line(s) in {path} (they'll be re-scanned).")

    return done


def append_result(path, tag, hits):
    """
    Append one tag's results as a single line and flush it to disk.

    This is the crash-safe part: the file is only ever extended, so an
    interrupted run loses at most the tag currently being written.
    """
    record = json.dumps({"tag": tag, "hits": hits}, ensure_ascii=False)
    with _open_with_retry(path, "a") as f:
        f.write(record + "\n")
        f.flush()
        os.fsync(f.fileno())


def iter_latest_records(path):
    """
    Yield one record per tag, using the last occurrence if a tag was written
    more than once. Streams via file offsets so the whole dataset never has
    to sit in memory at once.
    """
    offsets = {}
    with open(path, "r", encoding="utf-8") as f:
        while True:
            pos = f.tell()
            line = f.readline()
            if not line:
                break
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            tag = record.get("tag")
            if tag:
                offsets[tag] = pos

    with open(path, "r", encoding="utf-8") as f:
        for tag, pos in offsets.items():
            f.seek(pos)
            yield json.loads(f.readline())


def write_combined_json(progress_path, out_path):
    """Fold the .jsonl into {tag: [hits...]} without loading it all at once."""
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")

    count = 0
    with open(tmp, "w", encoding="utf-8") as out:
        out.write("{\n")
        for record in iter_latest_records(progress_path):
            if count:
                out.write(",\n")
            key = json.dumps(record["tag"], ensure_ascii=False)
            val = json.dumps(record["hits"], ensure_ascii=False)
            out.write(f"  {key}: {val}")
            count += 1
        out.write("\n}\n")

    _replace_with_retry(tmp, out_path)
    print(f"Combined {count} tag(s) into {out_path}.")


################################################################################
# GIT HELPERS
################################################################################

def get_local_tags(repo_path=".", sort_by="version"):
    """
    Fetches local tags sorted.
    sort_by options:
      - "version": Highest semver first
      - "date": Most recently created tags first
    """
    sort_flag = "-version:refname" if sort_by == "version" else "-creatordate"

    try:
        # for-each-ref formats each line directly as: <tag_name> <commit_hash>
        result = subprocess.run(
            [
                "git", "for-each-ref",
                f"--sort={sort_flag}",
                "--format=%(refname:short) %(objectname)",
                "refs/tags"
            ],
            cwd=repo_path,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=True
        )
    except subprocess.CalledProcessError as e:
        print(f"Error reading local tags: {e.stderr}")
        return []

    tags = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue

        tag_name, commit_hash = line.split()
        tags.append({"name": tag_name, "commit": commit_hash})

    return tags


def search_tag(tag, grep_args):
    """
    Run git grep against a single tag.

    Returns a list of hits, or None if git itself failed (so the caller knows
    not to record this tag as 'done').
    """
    # Command: git grep -F -n -e "target1" -e "target2" <tag> -- src/
    cmd = grep_args + [tag, '--', 'src/']

    # encoding is pinned to utf-8: Lean sources are full of Unicode and the
    # Windows locale default (cp1252) cannot decode them.
    res = subprocess.run(
        cmd,
        cwd=REPO,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False
    )

    # git grep: 0 = matches found, 1 = no matches (not an error), >1 = real error
    if res.returncode > 1:
        print(f"  git grep failed for {tag}: {res.stderr.strip()}")
        return None

    tag_data = []
    if not res.stdout:
        return tag_data

    for line in res.stdout.strip().split('\n'):
        # git grep output format when targeting a ref:
        # <tag>:<file_path>:<line_number>:<content>
        parts = line.split(':', 3)

        if len(parts) != 4:
            continue

        _, file_path, line_num, content = parts

        # Figure out exactly which targets matched in this line
        found_targets = [
            target for target, regex in TARGET_REGEXES.items()
            if regex.search(content)
        ]

        if found_targets:
            tag_data.append({
                "file": file_path,
                "line": int(line_num),
                "targets": found_targets,
                "content": content.strip()
            })

    return tag_data


################################################################################
# MAIN
################################################################################

def main():
    if not os.path.exists(REPO):
        print(f"Repo not found: {REPO}")
        return

    local_tags = get_local_tags(REPO)
    if not local_tags:
        print("No tags found.")
        return

    print(f"Found {len(local_tags)} tags.")

    # If you have a specific list of tags, replace this with your own list:
    # tags = ["v1.0.0", "v1.1.0"]
    tags = [tag['name'] for tag in local_tags]

    if RESUME:
        done = load_done_tags(PROGRESS_FILE)
        if done:
            print(f"Resuming: {len(done)} tag(s) already in {PROGRESS_FILE}.")
    else:
        done = set()
        if PROGRESS_FILE.exists():
            PROGRESS_FILE.unlink()

    # Build the base git grep command: fixed strings (-F), line numbers (-n)
    grep_args = ['git', '-c', 'core.quotepath=false', 'grep', '-F', '-n']
    for target in TARGETS:
        grep_args.extend(['-e', target])

    failed = []

    for i, tag in enumerate(tags, start=1):
        if tag in done:
            print(f"[{i}/{len(tags)}] Skipping {tag} (already saved).")
            continue

        print(f"[{i}/{len(tags)}] Searching {tag}...")
        tag_data = search_tag(tag, grep_args)

        if tag_data is None:
            failed.append(tag)
            continue

        # Only record once the tag has been searched successfully.
        append_result(PROGRESS_FILE, tag, tag_data)
        done.add(tag)
        print(f"  {len(tag_data)} hit(s) -> saved ({len(done)}/{len(tags)} tags done).")

    print(f"\nDone! {len(done)}/{len(tags)} tags in {PROGRESS_FILE}.")

    if failed:
        print(f"{len(failed)} tag(s) failed and were not saved: {', '.join(failed)}")
        print("Re-run the script to retry just those.")

    if WRITE_COMBINED_JSON and done:
        write_combined_json(PROGRESS_FILE, OUTPUT_FILE)


if __name__ == "__main__":
    main()
