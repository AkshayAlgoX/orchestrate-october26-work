"""Loaders for the participant-facing CSV inputs and the identity index."""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
USER_REF_RE = re.compile(r"^user_\d+$")


def norm_email(value: str) -> str:
    return value.strip().lower()


def norm_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    return digits if len(digits) >= 9 else ""


def norm_token(value) -> str:
    """Normalize an identifier-like value for exact matching.

    Emails and hashes compare case-insensitively; phones compare on digits;
    everything else compares on the trimmed, lower-cased string. No fuzzy
    matching is ever applied.
    """
    if value is None:
        return ""
    s = str(value).strip()
    if not s or s.lower() in ("null", "none", "-", "—"):
        return ""
    if "@" in s:
        return norm_email(s)
    if re.fullmatch(r"\+?[\d\s().-]{9,}", s):
        p = norm_phone(s)
        return "tel:" + p if p else ""
    return s.lower()


def read_csv(path: Path) -> List[Dict[str, str]]:
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


@dataclass
class Identity:
    user_id: str
    name: str
    current_email: str
    emails: Set[str] = field(default_factory=set)     # normalized, current + historical
    hashes: Set[str] = field(default_factory=set)
    phones: Set[str] = field(default_factory=set)     # normalized "tel:<digits>"
    names: Set[str] = field(default_factory=set)      # corroboration only, never a link by itself

    @property
    def link_tokens(self) -> Set[str]:
        return self.emails | self.hashes | self.phones | {self.user_id.lower()}


class IdentityIndex:
    def __init__(self, identities_rows, audit_rows):
        self.by_user: Dict[str, Identity] = {}
        for row in identities_rows:
            ident = Identity(row["user_id"], row["name"], norm_email(row["email"]))
            ident.emails.add(norm_email(row["email"]))
            if row.get("email_hash"):
                ident.hashes.add(row["email_hash"].strip().lower())
            if norm_phone(row.get("phone", "")):
                ident.phones.add("tel:" + norm_phone(row["phone"]))
            ident.names.add(row["name"].strip().lower())
            self.by_user[row["user_id"]] = ident
        for row in audit_rows:
            ident = self.by_user.get(row["user_id"])
            if ident is None:
                continue
            for value in (row["old_value"], row["new_value"]):
                if not value:
                    continue
                if row["field_name"] == "email":
                    ident.emails.add(norm_email(value))
                elif row["field_name"] == "email_hash":
                    ident.hashes.add(value.strip().lower())
                elif row["field_name"] == "phone" and norm_phone(value):
                    ident.phones.add("tel:" + norm_phone(value))
                elif row["field_name"] == "name":
                    ident.names.add(value.strip().lower())
        self.token_owner: Dict[str, Set[str]] = defaultdict(set)
        for uid, ident in self.by_user.items():
            for tok in ident.link_tokens:
                self.token_owner[tok].add(uid)

    def resolve_email(self, email: str) -> Optional[str]:
        owners = self.token_owner.get(norm_email(email), set())
        return next(iter(owners)) if len(owners) == 1 else None

    def owners_of(self, tokens) -> Set[str]:
        out: Set[str] = set()
        for tok in tokens:
            out |= self.token_owner.get(tok, set())
        return out


@dataclass
class Dataset:
    root: Path
    requests: List[Dict[str, str]]
    identities: IdentityIndex
    activity: List[Dict[str, str]]
    rules: List[Dict[str, str]]
    manifest: Dict[str, Dict[str, str]]
    audit_events: Dict[str, str] = field(default_factory=dict)   # audit_id -> event_time (citable ids)


# Columns each participant CSV must provide (the contract headers in dataset/).
REQUEST_COLUMNS = ("case_id", "email", "requested_at", "reported_status", "reported_at", "audit_at", "request_text")
REQUIRED_COLUMNS = {
    "user_identities.csv": ("user_id", "name", "email", "phone", "email_hash", "updated_at"),
    "user_audit_log.csv": ("audit_id", "user_id", "event_time", "field_name", "old_value", "new_value"),
    "system_activity.csv": ("activity_id", "event_time", "event_type", "source_system", "target_system",
                            "resource_id", "reported_result", "details"),
    "deletion_rules.csv": ("rule_id", "data_category", "condition", "required_action"),
    "evidence_files.csv": ("file_id", "file_path", "origin", "captured_at"),
}


def missing_columns(path: Path, required) -> List[str]:
    """Required columns absent from the header of a CSV file."""
    with open(path, encoding="utf-8", newline="") as fh:
        header = next(csv.reader(fh), [])
    return [c for c in required if c not in header]


def load_dataset(dataset_dir: Path, requests_path: Optional[Path] = None) -> Dataset:
    dataset_dir = Path(dataset_dir)
    requests = read_csv(requests_path or dataset_dir / "deletion_requests.csv")
    audit_log = read_csv(dataset_dir / "user_audit_log.csv")
    identities = IdentityIndex(read_csv(dataset_dir / "user_identities.csv"), audit_log)
    activity = read_csv(dataset_dir / "system_activity.csv")
    rules = read_csv(dataset_dir / "deletion_rules.csv")
    manifest = {r["file_id"]: r for r in read_csv(dataset_dir / "evidence_files.csv")}
    audit_events = {r["audit_id"]: r["event_time"] for r in audit_log if r.get("audit_id")}
    return Dataset(dataset_dir, requests, identities, activity, rules, manifest, audit_events)
