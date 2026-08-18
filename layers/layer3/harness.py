"""Two ways to get diagnostics out of a live toolchain.

`lean --json` is the right default: one process per file, NDJSON on stdout, no
protocol state. Use it for bulk corpus sweeps.

The LSP driver exists for the cases --json cannot reach: incremental edits, goal
states, anything where you need the message as the editor sees it. One caveat worth
stating plainly -- `collectDiagnostics` is the *interactive test driver's* directive
spelling, not an LSP method. At the wire level you open the document, wait for the
file to finish processing, and collect the `textDocument/publishDiagnostics`
notifications that arrive. `$/lean/waitForDiagnostics` is the request that gives you
"finished processing"; we probe for it and fall back to a quiescence timer if the
server does not answer, since custom request names have moved between releases.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Iterable, Optional

from ..schema import Position, RawDiagnostic

_SEV_NUM = {1: "error", 2: "warning", 3: "information", 4: "trace"}


# --- lean --json -----------------------------------------------------------

def lean_cmd(toolchain: str | None, lake_env: bool = False) -> list[str]:
    """Resolve `lean` for a toolchain.

    `lake_env=True` prefixes `lake env`, which is REQUIRED for any file importing
    from its own package or a dependency: bare `lean` has no LEAN_PATH, so every
    import fails and the whole sweep silently degenerates into thousands of
    identical "unknown module" errors that look like a real finding. Use it for
    package corpora; skip it for standalone files importing nothing beyond core.
    """
    pinned = os.environ.get("LAYER3_PINNED_TOOLCHAIN") == toolchain
    prefix: list[str] = [] if (not toolchain or pinned) else ["elan", "run", toolchain]
    return prefix + (["lake", "env", "lean"] if lake_env else ["lean"])


def _coerce_pos(v) -> Optional[Position]:
    if not isinstance(v, dict):
        return None
    # `lean --json` uses 1-based line / 0-based column; LSP uses 0-based both.
    # Normalize to LSP.
    if "line" in v and "column" in v:
        return Position(max(int(v["line"]) - 1, 0), int(v["column"]))
    if "line" in v and "character" in v:
        return Position(int(v["line"]), int(v["character"]))
    return None


def run_json(path: Path, toolchain: str = "", extra_flags: Iterable[str] = (),
             cwd: Optional[Path] = None, timeout: int = 600,
             lake_env: bool = False) -> list[RawDiagnostic]:
    """Elaborate one file and return its diagnostics.

    The JSON schema is read defensively. Field names here have moved across
    releases (`data` vs `message`, `pos` vs `range`), so every access is a `.get`
    with a fallback and unknown keys are preserved in `extra` rather than dropped.
    """
    cmd = lean_cmd(toolchain, lake_env) + ["--json", *extra_flags, str(path)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, cwd=str(cwd) if cwd else None)
    except subprocess.TimeoutExpired:
        return [RawDiagnostic(severity="error", text="<layer3: elaboration timeout>",
                              file=str(path), source="lean --json",
                              toolchain=toolchain, extra={"synthetic": True})]

    out: list[RawDiagnostic] = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line or line[0] != "{":
            continue
        try:
            v = json.loads(line)
        except json.JSONDecodeError:
            continue
        sev = v.get("severity")
        if isinstance(sev, int):
            sev = _SEV_NUM.get(sev, "unknown")
        text = v.get("data") or v.get("message") or ""
        if not isinstance(text, str):
            text = json.dumps(text, ensure_ascii=False)
        known = {"severity", "data", "message", "pos", "endPos", "range",
                 "fileName", "caption", "code", "errorName"}
        out.append(RawDiagnostic(
            severity=sev if sev in _SEV_NUM.values() else "unknown",  # type: ignore[arg-type]
            text=text,
            file=v.get("fileName") or str(path),
            start=_coerce_pos(v.get("pos") or (v.get("range") or {}).get("start")),
            end=_coerce_pos(v.get("endPos") or (v.get("range") or {}).get("end")),
            error_name=v.get("errorName") or (
                v.get("code") if isinstance(v.get("code"), str) else None),
            source="lean --json",
            toolchain=toolchain,
            extra={k: val for k, val in v.items() if k not in known},
        ))
    if r.returncode != 0 and not out:
        out.append(RawDiagnostic(severity="error",
                                 text=f"<layer3: exit {r.returncode}>\n{r.stderr[:2000]}",
                                 file=str(path), source="lean --json",
                                 toolchain=toolchain, extra={"synthetic": True}))
    return out


# --- LSP -------------------------------------------------------------------

class LeanServer:
    """Minimal LSP client. Content-Length framing, one worker file at a time."""

    def __init__(self, root: Path, toolchain: str = "", timeout: float = 300.0):
        self.root, self.toolchain, self.timeout = root, toolchain, timeout
        self._id = 0
        self._diags: dict[str, list[dict]] = {}
        self._responses: dict[int, dict] = {}
        self._lock = threading.Lock()
        self._proc = subprocess.Popen(
            lean_cmd(toolchain) + ["--server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd=str(root))
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        f = self._proc.stdout
        assert f is not None
        while True:
            length = 0
            while True:
                line = f.readline()
                if not line:
                    return
                if line in (b"\r\n", b"\n"):
                    break
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":")[1])
            if not length:
                continue
            try:
                msg = json.loads(f.read(length).decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            with self._lock:
                if msg.get("method") == "textDocument/publishDiagnostics":
                    p = msg.get("params", {})
                    self._diags[p.get("uri", "")] = p.get("diagnostics", [])
                elif "id" in msg and "method" not in msg:
                    self._responses[msg["id"]] = msg

    def _send(self, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        assert self._proc.stdin is not None
        self._proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        self._proc.stdin.flush()

    def request(self, method: str, params: dict, wait: bool = True) -> Optional[dict]:
        self._id += 1
        rid = self._id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        if not wait:
            return None
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            with self._lock:
                if rid in self._responses:
                    return self._responses.pop(rid)
            time.sleep(0.01)
        return None

    def notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def initialize(self) -> dict:
        resp = self.request("initialize", {
            "processId": os.getpid(),
            "rootUri": self.root.as_uri(),
            "capabilities": {"textDocument": {"publishDiagnostics": {}}},
        }) or {}
        self.notify("initialized", {})
        return resp

    def diagnostics_for(self, path: Path, quiesce: float = 1.5) -> list[RawDiagnostic]:
        uri = path.as_uri()
        text = path.read_text(encoding="utf-8", errors="replace")
        with self._lock:
            self._diags.pop(uri, None)
        self.notify("textDocument/didOpen", {"textDocument": {
            "uri": uri, "languageId": "lean4", "version": 1, "text": text}})

        # Preferred: ask the server when the file is done. If the method is not
        # recognized the response carries an error and we fall through.
        resp = self.request("$/lean/waitForDiagnostics", {
            "uri": uri, "version": 1}) or {}
        settled = "error" not in resp

        if not settled:
            # Fallback: wait for the diagnostics set to stop changing.
            last, stable_since = None, time.time()
            deadline = time.time() + self.timeout
            while time.time() < deadline:
                with self._lock:
                    cur = json.dumps(self._diags.get(uri, []), sort_keys=True)
                if cur != last:
                    last, stable_since = cur, time.time()
                elif time.time() - stable_since > quiesce:
                    break
                time.sleep(0.05)

        with self._lock:
            raw = list(self._diags.get(uri, []))
        self.notify("textDocument/didClose", {"textDocument": {"uri": uri}})

        out: list[RawDiagnostic] = []
        for d in raw:
            rng = d.get("fullRange") or d.get("range") or {}
            st, en = rng.get("start", {}), rng.get("end", {})
            out.append(RawDiagnostic(
                severity=_SEV_NUM.get(d.get("severity"), "unknown"),  # type: ignore[arg-type]
                text=d.get("message", ""),
                file=str(path),
                start=Position(st.get("line", 0), st.get("character", 0)),
                end=Position(en.get("line", 0), en.get("character", 0)),
                error_name=d.get("code") if isinstance(d.get("code"), str) else None,
                source="lsp",
                toolchain=self.toolchain,
                extra={"settled_via": "waitForDiagnostics" if settled else "quiescence"},
            ))
        return out

    def close(self) -> None:
        try:
            self.request("shutdown", {}, wait=True)
            self.notify("exit", {})
        finally:
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()

    def __enter__(self) -> "LeanServer":
        self.initialize()
        return self

    def __exit__(self, *exc) -> None:
        self.close()
