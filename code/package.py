"""Build and verify a reproducible, allowlist-based code.zip.

Usage:
  python3 code/package.py --out /tmp/code.zip          # build a candidate, then verify it
  python3 code/package.py --verify code.zip            # verify an existing archive only

Layout inside the archive:
  README.md
  code/**.py                         (source, evaluation harness, tests, packager)
  code/evaluation/*.md               (README, evaluation_report.md, usage_report.md)
  code/evaluation/sample_predictions.csv
  evaluation/evaluation_report.md    (byte-identical copy; problem_statement.md / AGENTS.md path)
  evaluation/usage_report.md         (byte-identical copy)

Never included: dataset/, output.csv, log.txt, gold answers, caches, virtualenvs, archives,
credentials. Entries are sorted and timestamped 1980-01-01 so identical inputs give
byte-identical archives.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import zipfile
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
FIXED_TIME = (1980, 1, 1, 0, 0, 0)
REPORTS = ("evaluation_report.md", "usage_report.md")
FORBIDDEN_PARTS = ("dataset/", "__pycache__", ".venv/", "venv/", "node_modules/", ".git/", ".env",
                   "log.txt", "output.csv", "sample_output.csv", "agent-trace")
FORBIDDEN_SUFFIXES = (".zip", ".tar", ".gz", ".tgz", ".7z", ".rar", ".pyc", ".sqlite", ".db")
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"), re.compile(r"AKIA[0-9A-Z]{16}"), re.compile(r"ghp_[A-Za-z0-9]{30,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*['\"][^'\"\s]{12,}['\"]"),
    re.compile(r"/home/[a-z]+/"), re.compile(r"/Users/[A-Za-z]+/"),
]


PROMPT_FILES = ("visual_transcription.md", "intent_classification.md", "agent_system_prompt.md")
CONFIG_FILES = ("audit_config.json",)


def allowlist() -> list:
    files = [ROOT / "README.md"]
    for p in sorted((ROOT / "code").rglob("*")):
        rel = p.relative_to(ROOT).as_posix()
        if not p.is_file() or "__pycache__" in rel:
            continue
        if p.suffix == ".py" or (rel.startswith("code/evaluation/") and p.suffix == ".md") \
                or rel == "code/evaluation/sample_predictions.csv" \
                or (rel.startswith("code/prompts/") and p.suffix in (".md", ".txt")) \
                or (rel.startswith("code/config/") and p.suffix == ".json"):
            files.append(p)
    return files


def build(out: Path) -> None:
    entries = {p.relative_to(ROOT).as_posix(): p.read_bytes() for p in allowlist()}
    for name in REPORTS:
        src = ROOT / "code" / "evaluation" / name
        if not src.is_file():
            raise SystemExit(f"missing required report {src}")
        entries[f"evaluation/{name}"] = src.read_bytes()
    for name in PROMPT_FILES:
        src = ROOT / "code" / "prompts" / name
        if not src.is_file():
            raise SystemExit(f"missing required prompt file {src}")
        entries[f"prompts/{name}"] = src.read_bytes()
    for name in CONFIG_FILES:
        src = ROOT / "code" / "config" / name
        if not src.is_file():
            raise SystemExit(f"missing required config file {src}")
        entries[f"config/{name}"] = src.read_bytes()
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w") as zf:
        for name in sorted(entries):
            info = zipfile.ZipInfo(name, FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, entries[name], compresslevel=9)


def verify(path: Path) -> int:
    problems = []
    with zipfile.ZipFile(path) as zf:
        names = sorted(zf.namelist())
        blobs = {n: zf.read(n) for n in names}
    for n in names:
        if any(part in n for part in FORBIDDEN_PARTS) or n.endswith(FORBIDDEN_SUFFIXES):
            problems.append(f"forbidden entry: {n}")
    required = ["README.md", "code/main.py", "code/ghost_audit/audit.py", "code/ghost_audit/agent.py",
                "code/evaluation/main.py"] + [f"evaluation/{r}" for r in REPORTS] + \
               [f"code/evaluation/{r}" for r in REPORTS] + \
               [f"prompts/{p}" for p in PROMPT_FILES] + [f"code/prompts/{p}" for p in PROMPT_FILES] + \
               [f"config/{c}" for c in CONFIG_FILES] + [f"code/config/{c}" for c in CONFIG_FILES]
    for r in required:
        if r not in blobs:
            problems.append(f"missing required entry: {r}")
    for r in REPORTS:
        a, b = blobs.get(f"evaluation/{r}"), blobs.get(f"code/evaluation/{r}")
        if a is not None and a != b:
            problems.append(f"report copies differ: {r}")
    for p in PROMPT_FILES:
        a, b = blobs.get(f"prompts/{p}"), blobs.get(f"code/prompts/{p}")
        if a is not None and a != b:
            problems.append(f"prompt copies differ: {p}")
    for c in CONFIG_FILES:
        a, b = blobs.get(f"config/{c}"), blobs.get(f"code/config/{c}")
        if a is not None and a != b:
            problems.append(f"config copies differ: {c}")
    for n, data in blobs.items():
        text = data.decode("utf-8", errors="ignore")
        for pat in SECRET_PATTERNS:
            m = pat.search(text)
            if m:
                problems.append(f"possible secret/personal path in {n}: {m.group(0)[:40]!r}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    print(f"archive {path} sha256 {digest} entries {len(names)} bytes {path.stat().st_size}")
    for n in names:
        print(f"  {len(blobs[n]):8d}  {hashlib.sha256(blobs[n]).hexdigest()[:12]}  {n}")
    for p in problems:
        print("PROBLEM:", p)
    print("PACKAGE CHECK:", "PASSED" if not problems else f"FAILED ({len(problems)})")
    return 1 if problems else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", help="write the archive here, then verify it")
    ap.add_argument("--verify", help="verify an existing archive")
    args = ap.parse_args(argv)
    if args.out:
        build(Path(args.out))
        return verify(Path(args.out))
    if args.verify:
        return verify(Path(args.verify))
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
