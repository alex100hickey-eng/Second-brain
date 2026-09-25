"""Carry the ledger between machines without losing a row of gate evidence.

    python3 -m polybot.migrate snapshot --out DIR   # on the SOURCE, after its loop is stopped
    python3 -m polybot.migrate verify   --dir DIR   # on the TARGET, after copying DIR's files in place

`snapshot` makes a consistent copy of polybot.db with sqlite's online backup (never `cp` a live
sqlite file), copies the other runtime files, and writes manifest.json: a sha256 per file, sqlite's
integrity_check, and the GATE FINGERPRINT — per module, the signal count, decision count, closed
count and closed P&L since gate_since_ts, plus gate_since_ts itself. `verify` recomputes all of it
where the files landed and exits non-zero on any difference. gate_since_ts must come across unchanged:
a move is not a rule change and must not reset the evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time

from . import config

FILES = ("config.json", "pairs.json", "calibration.json", "calibration-samples.json", "jobs-state.json",
         "compounding-state.json")


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fingerprint(db_path: str, gate_since_ts: float) -> dict:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = con.execute(
        """SELECT s.module, COUNT(*),
                  COUNT(DISTINCT COALESCE(json_extract(s.meta,'$.group'), 'row:' || s.id)),
                  SUM(CASE WHEN p.status='closed' THEN 1 ELSE 0 END),
                  ROUND(COALESCE(SUM(CASE WHEN p.status='closed' THEN p.pnl_usd END), 0), 4)
           FROM signals s LEFT JOIN paper_trades p ON p.signal_id=s.id
           WHERE s.ts>=? AND s.status!='void' GROUP BY s.module ORDER BY s.module""", (gate_since_ts,)).fetchall()
    integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
    total = con.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    con.close()
    return {"gate_since_ts": gate_since_ts, "integrity": integrity, "signals_total": total,
            "modules": {m: {"signals": n, "decisions": d, "closed": c or 0, "closed_pnl": p}
                        for m, n, d, c, p in rows}}


def _gate_since(config_path: str) -> float:
    with open(config_path) as f:
        return float(json.load(f).get("gate_since_ts") or 0.0)


def snapshot(out_dir: str, data_dir: str = config.DATA_DIR) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    src = sqlite3.connect(os.path.join(data_dir, "polybot.db"))
    dst = sqlite3.connect(os.path.join(out_dir, "polybot.db"))
    src.backup(dst)
    dst.close()
    src.close()
    for name in FILES:
        if os.path.exists(os.path.join(data_dir, name)):
            shutil.copy2(os.path.join(data_dir, name), os.path.join(out_dir, name))
    files = sorted(n for n in ("polybot.db",) + FILES if os.path.exists(os.path.join(out_dir, n)))
    manifest = {"files": {n: sha256(os.path.join(out_dir, n)) for n in files},
                "fingerprint": fingerprint(os.path.join(out_dir, "polybot.db"),
                                           _gate_since(os.path.join(out_dir, "config.json")))}
    with open(os.path.join(out_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=1)
    return manifest


def verify(snapshot_dir: str, data_dir: str = config.DATA_DIR) -> list:
    """Problems (empty = identical). Compares the files IN data_dir against the snapshot's manifest."""
    with open(os.path.join(snapshot_dir, "manifest.json")) as f:
        manifest = json.load(f)
    problems = []
    for name, digest in manifest["files"].items():
        p = os.path.join(data_dir, name)
        if not os.path.exists(p):
            problems.append(f"{name}: missing")
        elif sha256(p) != digest:
            problems.append(f"{name}: sha256 differs")
    fp = fingerprint(os.path.join(data_dir, "polybot.db"), _gate_since(os.path.join(data_dir, "config.json")))
    want = manifest["fingerprint"]
    if fp["gate_since_ts"] != want["gate_since_ts"]:
        problems.append(f"gate_since_ts changed: {want['gate_since_ts']} -> {fp['gate_since_ts']}")
    if fp["integrity"] != "ok":
        problems.append(f"integrity_check: {fp['integrity']}")
    if fp["modules"] != want["modules"] or fp["signals_total"] != want["signals_total"]:
        problems.append("gate fingerprint differs (signals / decisions / closed / P&L)")
    return problems


MARKER = "MOVE_VERIFIED"      # polybot_supervisor.MARKER: the server loop starts only once this exists


def mark(data_dir: str, manifest_dir: str) -> str:
    with open(os.path.join(manifest_dir, "manifest.json")) as f:
        manifest = json.load(f)
    path = os.path.join(data_dir, MARKER)
    with open(path, "w") as f:
        json.dump({"verified_at": time.time(), "polybot.db": manifest["files"].get("polybot.db"),
                   "gate_since_ts": manifest["fingerprint"]["gate_since_ts"]}, f)
    return path


def unmark(data_dir: str) -> bool:
    try:
        os.remove(os.path.join(data_dir, MARKER))
        return True
    except FileNotFoundError:
        return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="polybot.migrate")
    ap.add_argument("cmd", choices=["snapshot", "verify", "unmark"])
    ap.add_argument("--out")
    ap.add_argument("--dir")
    ap.add_argument("--mark", action="store_true", help=f"verify: on OK, write {MARKER} (the server loop's go-ahead)")
    a = ap.parse_args(argv)
    if a.cmd == "unmark":
        print(f"{MARKER} removed" if unmark(config.DATA_DIR) else f"no {MARKER} in {config.DATA_DIR}")
        return 0
    if a.cmd == "snapshot":
        m = snapshot(a.out, data_dir=config.DATA_DIR)
        print(json.dumps(m["fingerprint"], indent=1))
        print(f"snapshot written to {a.out} ({len(m['files'])} files)")
        return 0
    problems = verify(a.dir, data_dir=config.DATA_DIR)
    print("verify: OK — every file and the gate fingerprint match" if not problems else
          "verify: FAILED\n  " + "\n  ".join(problems))
    if not problems and a.mark:
        print(f"marked: {mark(config.DATA_DIR, a.dir)}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
