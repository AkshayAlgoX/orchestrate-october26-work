"""Ordering, serialization and contract self-checks for output.csv."""

from __future__ import annotations

import contextlib
import csv
import io
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .model import OP_RANK, PHASE_OF, STATE_OF, STEP_FIELDS, Step

HEADER = ["case_id", "deletion_status", "steps_json"]


def case_sort_key(case_id: str):
    """Natural order (case_33 < case_100), matching deletion_requests.csv.

    The contract says "sorted by case_id" without defining string vs numeric
    order; the organizers' own request file is in natural numeric order, so
    that interpretation is used (documented in the evaluation report).
    """
    m = re.search(r"(\d+)$", case_id)
    return (case_id[: m.start()] if m else case_id, int(m.group(1)) if m else -1, case_id)


def order_steps(steps: List[Step]) -> List[Step]:
    unique: Dict[Tuple, Step] = {}
    for s in steps:
        key = (s.phase, s.system_id, s.resource_id, s.operation)
        if key not in unique or (s.evidence_id and not unique[key].evidence_id):
            unique[key] = s
    return sorted(unique.values(), key=lambda s: s.sort_key())


def to_json(steps: List[Step]) -> str:
    payload = []
    for i, s in enumerate(order_steps(steps), 1):
        assert PHASE_OF[s.operation] == s.phase, (s.operation, s.phase)
        payload.append({"step_no": i, "phase": s.phase, "system_id": s.system_id, "resource_id": s.resource_id,
                        "operation": s.operation, "state": STATE_OF[s.operation], "evidence_id": s.evidence_id})
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def check_row(case_id: str, status: str, steps_json: str) -> List[str]:
    """Contract self-check for one output row. Never raises on malformed input."""
    errors = []
    try:
        steps = json.loads(steps_json)
    except (TypeError, ValueError) as exc:
        return [f"{case_id}: steps_json is not valid JSON ({exc})"]
    if not isinstance(steps, list):
        return [f"{case_id}: steps_json must be a JSON array, got {type(steps).__name__}"]
    if status not in ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"):
        errors.append(f"{case_id}: bad status {status}")
    if status == "NOT_APPLICABLE" and steps:
        errors.append(f"{case_id}: NOT_APPLICABLE with steps")
    prev = None
    for i, s in enumerate(steps, 1):
        if not isinstance(s, dict):
            errors.append(f"{case_id}: step {i} must be a JSON object, got {type(s).__name__}")
            continue
        if tuple(sorted(s)) != tuple(sorted(STEP_FIELDS)):
            errors.append(f"{case_id}: step {i} fields {sorted(s)}")
            continue
        if s["step_no"] != i:
            errors.append(f"{case_id}: step numbering")
        if s["operation"] not in OP_RANK or s["phase"] not in ("FINDING", "REMEDIATION", "VERIFICATION") \
                or PHASE_OF.get(s["operation"]) != s["phase"]:
            errors.append(f"{case_id}: invalid phase/operation at {i}")
            continue
        if STATE_OF.get(s["operation"]) != s["state"]:
            errors.append(f"{case_id}: state mismatch at {i}")
        key = (("FINDING", "REMEDIATION", "VERIFICATION").index(s["phase"]), OP_RANK[s["operation"]],
               s["system_id"], s["resource_id"])
        if prev and key < prev:
            errors.append(f"{case_id}: ordering at {i}")
        prev = key
        future = status == "INCOMPLETE" and s["phase"] != "FINDING"
        if future and s["evidence_id"]:
            errors.append(f"{case_id}: future step with evidence")
        if not future and status != "NOT_APPLICABLE" and not s["evidence_id"]:
            errors.append(f"{case_id}: missing citation at {i}")
    if status == "COMPLETE" and any(isinstance(s, dict) and s.get("phase") != "VERIFICATION" for s in steps):
        errors.append(f"{case_id}: COMPLETE with non-verification steps")
    return errors


CASE_ORDERS = ("natural", "lexicographic")


def serialize_csv(rows: List[Tuple[str, str, str]], case_order: str = "natural") -> str:
    """output.csv text with rows in a deterministic case order.

    "natural" (default) matches dataset/deletion_requests.csv (case_33 ... case_550);
    "lexicographic" is plain string order (case_100 ... case_99). The contract says only
    "sorted by case_id"; see the evaluation report (sprint 3, scenario E).
    """
    if case_order not in CASE_ORDERS:
        raise ValueError(f"case_order must be one of {CASE_ORDERS}")
    key = (lambda r: case_sort_key(r[0])) if case_order == "natural" else (lambda r: r[0])
    rows = sorted(rows, key=key)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    w.writerow(HEADER)
    for r in rows:
        w.writerow(r)
    return buf.getvalue()


def write_csv(path, rows: List[Tuple[str, str, str]], case_order: str = "natural") -> None:
    """Write rows directly (no validation or atomic replacement; the CLI uses publish_csv)."""
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(serialize_csv(rows, case_order))


class PublicationError(RuntimeError):
    """The output could not be written and verified; the destination was left unchanged."""


def validate_csv_file(path, expected_ids: Iterable[str]) -> List[str]:
    """Re-read a written output file: exact header, exactly the expected case ids once each, valid rows."""
    problems: List[str] = []
    ids: List[str] = []
    with open(path, encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        if header != HEADER:
            problems.append(f"header is {header!r}, expected {HEADER!r}")
        for n, row in enumerate(reader, 2):
            if len(row) != 3:
                problems.append(f"line {n}: {len(row)} fields")
                continue
            ids.append(row[0])
            problems += check_row(row[0], row[1], row[2])
    expected = sorted(expected_ids)
    if len(ids) != len(set(ids)):
        problems.append("duplicate case_id rows")
    if sorted(set(ids)) != sorted(set(expected)) or len(ids) != len(expected):
        problems.append(f"{len(ids)} rows written for {len(expected)} requests, or the case ids differ")
    return problems


def _replace_atomically(dest: Path, text: str, verify=None) -> None:
    """Write `text` to a temporary file beside `dest`, optionally verify it, then rename it over `dest`.
    On any failure `dest` is untouched and the temporary file is removed. A symlinked destination is
    resolved so its target is replaced and the link kept. A destination that exists but is not a regular
    file (e.g. /dev/stdout) cannot be renamed over: the text is verified in a private temporary
    directory first and then written to it directly."""
    dest = Path(os.path.realpath(dest))
    if dest.is_dir():
        raise PublicationError(f"{dest} is a directory")
    if dest.exists() and not dest.is_file():
        with tempfile.TemporaryDirectory() as tmpdir:
            probe = Path(tmpdir) / dest.name
            probe.write_text(text, encoding="utf-8", newline="")
            problems = verify(str(probe)) if verify is not None else []
        if problems:
            raise PublicationError(f"written file failed verification: {problems[:3]}")
        with open(dest, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        return
    fd, tmp = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".tmp", dir=str(dest.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if dest.exists():
            os.chmod(tmp, stat.S_IMODE(dest.stat().st_mode))      # keep the destination's permissions
        else:
            umask = os.umask(0)
            os.umask(umask)
            os.chmod(tmp, 0o666 & ~umask)                         # as a plain open() would create it
        if verify is not None:
            problems = verify(tmp)
            if problems:
                raise PublicationError(f"written file failed verification: {problems[:3]}")
        os.replace(tmp, dest)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    if hasattr(os, "O_DIRECTORY"):   # make the rename durable where the platform allows it
        with contextlib.suppress(OSError):
            dirfd = os.open(str(dest.parent), os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)


def publish_csv(path, rows: List[Tuple[str, str, str]], case_order: str = "natural",
                expected_ids: Optional[Iterable[str]] = None) -> None:
    """All-or-nothing publication of output.csv: serialize, write beside the destination, re-read and
    validate the written bytes, then atomically replace the destination. Raises PublicationError (or
    OSError) with the destination unchanged."""
    expected = list(expected_ids) if expected_ids is not None else [r[0] for r in rows]
    _replace_atomically(Path(path), serialize_csv(rows, case_order), lambda p: validate_csv_file(p, expected))


def publish_text(path, text: str) -> None:
    """Atomic replacement for diagnostic files (no content validation)."""
    _replace_atomically(Path(path), text)
