"""Evidence parsers: every manifest entry -> typed Observations and Scopes.

Each parser reads one physical format and normalizes it into Observation
objects that keep file capture time, record observation time, the physical
store and a locator. Interpretation of a record (category, existence, exposed
identifiers) happens in `interpret`, which works on normalized field names so
that the same rules apply across SQL, JSON, logs, text, PDFs and screenshots.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .loaders import EMAIL_RE, USER_REF_RE, norm_token
from .model import Observation, Scope

RESOURCE_RE = re.compile(
    r"\b((?:account|search|analytics|support|export|data_flow|backup|audit_record|tombstone|"
    r"legal_hold|object|marketing|cache|email_delivery)_\d+)\b")
TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)")
PHONE_TEXT_RE = re.compile(r"\+\d[\d ()-]{7,}\d")

# Field names that carry direct personal identifiers when non-null.
PII_FIELDS = {
    "email": "email", "owner_email": "email", "requester_email": "email", "customer_email": "email",
    "full_name": "name", "name": "name", "display_name": "name", "requester_name": "name",
    "phone": "phone", "owner_phone": "phone", "requester_phone": "phone",
    "email_hash": "email_hash",
}
# Field names that carry a stable subject reference (user_id style or identifier).
REF_FIELDS = ("customer_reference", "subject_reference", "user_id", "subject_ref")
# Field names whose values are lookup/correlation identifiers (linking only).
KEY_FIELDS = ("lookup_key", "subject_key", "scan_key", "owner_key", "match_key", "correlation_key",
              "query", "lookup_value", "subject_selector", "uploaded_by", "matched_value")

SYSTEM_CATEGORY = {
    "account_service": "USER_PROFILE",
    "search_service": "SEARCH_INDEX_ENTRY",
    "analytics_platform": "ANALYTICS_PROFILE",
    "export_service": "USER_EXPORT",
    "integration_service": "DATA_FLOW",
    "backup_service": "BACKUP_ARCHIVE",
    "security_service": "SECURITY_AUDIT_LOG",
    "legal_service": "LEGAL_HOLD_RECORD",
    "identity_service": "ACCOUNT_TOMBSTONE",
    "marketing_platform": "MARKETING_PROFILE",
    "cache_service": "CACHE_ENTRY",
    "email_delivery_service": "EMAIL_DELIVERY_LOG",
}


def _null(v) -> bool:
    return v is None or (isinstance(v, str) and v.strip().lower() in ("", "null", "none", "—", "-"))


def _bool(v) -> Optional[bool]:
    if isinstance(v, bool):
        return v
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("true", "1", "yes", "allowed"):
        return True
    if s in ("false", "0", "no", "denied", "deny"):
        return False
    return None


@dataclass
class FileMeta:
    file_id: str
    path: Path
    captured_at: str
    origin: str
    parse_status: str = "pending"
    observations: int = 0
    note: str = ""
    image: Dict[str, object] = field(default_factory=dict)   # PNG provenance (hash, decode check, size)


# Parse statuses meaning the file's contents were read; anything else is an evidence gap.
PARSED = frozenset({"parsed", "parsed_visual_cache"})


@dataclass
class EvidenceStore:
    files: Dict[str, FileMeta] = field(default_factory=dict)
    observations: List[Observation] = field(default_factory=list)
    scopes: List[Scope] = field(default_factory=list)
    # document reference (e.g. statement number) -> list of (file_id, Observation-like dict)
    documents: Dict[str, List[dict]] = field(default_factory=lambda: defaultdict(list))

    def gaps(self) -> List[FileMeta]:
        """Manifest files whose contents could not be read. Their evidence is missing from every case,
        so an absence or a COMPLETE verdict computed without them is not established."""
        return [m for m in self.files.values() if m.parse_status not in PARSED]

    def by_resource(self) -> Dict[str, List[Observation]]:
        out: Dict[str, List[Observation]] = defaultdict(list)
        for ob in self.observations:
            out[ob.resource_id].append(ob)
        return out


# ---------------------------------------------------------------------------
# Interpretation shared by all formats
# ---------------------------------------------------------------------------

def interpret(meta: FileMeta, *, system: str, resource_id: str, store: str, locator: str,
              observed_at: str, fields: dict, extra_links: Iterable[str] = (),
              category: Optional[str] = None, exists: Optional[bool] = None,
              source_kind: str = "text") -> Observation:
    fields = {k: v for k, v in fields.items()}
    pii: Dict[str, str] = {}
    refs: List[str] = []
    links = set()
    for key, val in fields.items():
        if _null(val):
            continue
        if key in PII_FIELDS:
            pii[PII_FIELDS[key]] = str(val)
            links.add(norm_token(val))
        elif key in REF_FIELDS:
            refs.append(str(val))
            links.add(norm_token(val))
        elif key in KEY_FIELDS:
            links.add(norm_token(val))
    # Free text can still expose identifiers (support messages, documents).
    for key in ("message_text", "message_excerpt", "text"):
        txt = fields.get(key)
        if isinstance(txt, str) and txt:
            for em in EMAIL_RE.findall(txt):
                pii["email"] = em
                links.add(norm_token(em))
            for ph in PHONE_TEXT_RE.findall(txt):
                pii["phone"] = ph
                links.add(norm_token(ph))
    if str(fields.get("direct_identifiers", "")).strip() == "1" and not pii:
        pii["reported"] = "direct_identifier"
    if fields.get("detector") == "direct_identifier" and fields.get("matched_value"):
        pii["matched"] = str(fields["matched_value"])
    links.update(norm_token(v) for v in extra_links)
    links.discard("")

    # Category: explicit record_type wins, then store/event semantics, then system.
    cat = category or fields.get("record_type")
    if not cat:
        if system == "support_platform":
            if (fields.get("storage_class") == "ticket_attachment" or "attachment" in store
                    or str(fields.get("event", "")).startswith("attachment") or fields.get("file_name")):
                cat = "SUPPORT_ATTACHMENT"
            elif "contact_index" in store or str(fields.get("event", "")).startswith("contact_query"):
                cat = "SECONDARY_IDENTIFIER"
            else:
                cat = "SUPPORT_MESSAGE"
        elif system == "analytics_platform" and (_bool(fields.get("email_opt_in")) is not None
                                                 or str(fields.get("event", "")).startswith("contact_lookup")):
            cat = "MARKETING_PROFILE"
        else:
            cat = SYSTEM_CATEGORY.get(system)

    flow = None
    if "enabled" in fields:
        flow = _bool(fields.get("enabled"))
    restore = None
    for key in ("restore_allowed",):
        if key in fields:
            restore = _bool(fields[key])
    if fields.get("event") == "restore_candidate_evaluated":
        if _bool(fields.get("eligible")) and _bool(fields.get("deletion_block")) is False:
            restore = True
        elif _bool(fields.get("deletion_block")):
            restore = False
    if fields.get("event") == "restore_authorization_completed":
        restore = False if str(fields.get("decision", "")).lower() == "deny" else True

    result = str(fields.get("result", "") or "").upper()
    if result in ("ACTIVE",):
        if cat == "BACKUP_ARCHIVE":
            restore = True
        else:
            flow = True
    elif result == "DISABLED":
        flow = False
    elif result == "BLOCKED":
        restore = False
    elif result == "IDENTIFIABLE" and not pii:
        pii["reported"] = "IDENTIFIABLE"

    if exists is None:
        if result in ("NOT_FOUND", "MISS", "ABSENT"):
            exists = False
        elif result:
            exists = True
        status = str(fields.get("http_status") or fields.get("status_code") or "")
        rows = str(fields.get("rows", ""))
        if status == "404" or (rows == "0" and fields.get("event", "").endswith("lookup_completed")):
            exists = False
        elif status == "200":
            exists = True
        if "archive_present" in fields:
            exists = _bool(fields["archive_present"])
        if fields.get("available") is not None and cat == "USER_EXPORT":
            exists = True
    retention = fields.get("retention_until") or fields.get("retained_until")
    # Explicit record-to-record references (e.g. a search document's source_resource_id).
    references = [str(v) for k, v in fields.items()
                  if k != "resource_id" and k.endswith("_resource_id") and not _null(v)
                  and RESOURCE_RE.fullmatch(str(v))]
    return Observation(
        references=references,
        file_id=meta.file_id, file_captured_at=meta.captured_at, observed_at=observed_at or meta.captured_at,
        system=system, resource_id=resource_id, store=store, locator=locator, exists=exists,
        category=cat, pii=pii, refs=refs, link_values=frozenset(links), flow_enabled=flow,
        restore_allowed=restore, retention_until=retention, source_kind=source_kind)


# ---------------------------------------------------------------------------
# SQL snapshots
# ---------------------------------------------------------------------------

def _sql_captured(text: str) -> Optional[str]:
    m = re.search(r"Captured at:\s*(\S+)", text)
    return m.group(1) if m else None


def parse_sql(meta: FileMeta, store: EvidenceStore) -> None:
    text = meta.path.read_text(encoding="utf-8")
    con = sqlite3.connect(":memory:")
    con.executescript(text)
    header_time = _sql_captured(text) or meta.captured_at
    tables = [t for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    scope_systems: Dict[str, str] = {}
    if "snapshot_scope" in tables:
        scope_systems = dict(con.execute("SELECT origin_system, captured_at FROM snapshot_scope"))
    inventory_tables = {
        "customer_profiles": "account_service", "customer_search_documents": "search_service",
        "customer_analytics_profiles": "analytics_platform", "support_records": "support_platform",
        "customer_exports": "export_service", "connector_subscriptions": "integration_service",
        "backup_restore_entries": "backup_service", "security_audit_records": "security_service",
    }
    for table in tables:
        if table == "snapshot_scope":
            continue
        cols = [c[1] for c in con.execute(f'PRAGMA table_info("{table}")')]
        rows = [dict(zip(cols, r)) for r in con.execute(f'SELECT * FROM "{table}"')]
        per_system: Dict[str, set] = defaultdict(set)
        per_store: Dict[tuple, set] = defaultdict(set)   # (system, store_name) -> resource ids
        for i, row in enumerate(rows):
            if "record_json" in cols:
                rec = json.loads(row["record_json"])
                system = row["origin_system"]
                fields = _flatten_record(rec)
                coll = row.get("collection_name", "")
                observed = row.get("updated_at") or header_time
                store_name = f"{table}:{coll}"
            else:
                if table in inventory_tables:
                    system = inventory_tables[table]
                elif table.startswith("customer_profiles"):
                    system = "account_service"
                elif table.startswith("search_documents"):
                    system = "search_service"
                else:
                    continue
                fields = {k: v for k, v in row.items()}
                if "user_id" in fields:
                    fields["customer_reference"] = fields.pop("user_id")
                if system == "integration_service" and "enabled" in fields:
                    fields["enabled"] = bool(int(fields["enabled"]))
                if system == "backup_service" and "restore_allowed" in fields:
                    fields["restore_allowed"] = bool(int(fields["restore_allowed"]))
                observed = row.get("updated_at") or row.get("indexed_at") or header_time
                store_name = table
                coll = table
            rid = fields.get("resource_id") or row.get("resource_id")
            per_system[system].add(rid)
            per_store[(system, store_name)].add(rid)
            store.observations.append(interpret(
                meta, system=system, resource_id=rid, store=store_name, locator=f"{table}#row{i + 1}",
                observed_at=observed, fields=fields, exists=True, source_kind="sql"))
        # Scopes: inventory tables are scoped explicitly by snapshot_scope; other
        # tables are physical stores enumerating the systems they contain.
        if table in inventory_tables:
            sysname = inventory_tables[table]
            if sysname in scope_systems:
                store.scopes.append(Scope(meta.file_id, meta.captured_at, scope_systems[sysname], sysname,
                                          table, frozenset(per_system.get(sysname, set()))))
        else:
            # A physical table enumerates, per collection, the records it holds; the scope's
            # store name matches the observations' "table:collection" (or "table") identity.
            for (sysname, store_name), rids in sorted(per_store.items()):
                store.scopes.append(Scope(meta.file_id, meta.captured_at, header_time, sysname, store_name,
                                          frozenset(rids)))
    con.close()


def _flatten_record(rec: dict) -> dict:
    out = {}
    for k, v in rec.items():
        if isinstance(v, dict) and k in ("document", "profile"):
            for k2, v2 in v.items():
                out[k2] = v2
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# JSON records
# ---------------------------------------------------------------------------

def parse_json(meta: FileMeta, store: EvidenceStore) -> None:
    data = json.loads(meta.path.read_text(encoding="utf-8"))
    if "snapshots" in data:
        for si, snap in enumerate(data["snapshots"]):
            _json_collection(meta, store, snap["data_store"], snap["collection"], snap["captured_at"],
                             snap["records"], f"snapshots[{si}]")
    elif "service_exports" in data:
        _service_exports(meta, store, data)
    elif "collection" in data and "records" in data:
        system = {"analytics_identity_store": "analytics_platform",
                  "user_export_store": "export_service"}.get(data.get("source", ""), data.get("source", ""))
        _json_collection(meta, store, system, data["collection"], data.get("captured_at", meta.captured_at),
                         data["records"], "records")
    elif "records" in data:
        for i, rec in enumerate(data["records"]):
            _record_lookup(meta, store, rec, f"records[{i}]")
    else:
        meta.note = "unrecognized JSON layout"


OPERATIONAL_STORES = {"operational_record_history"}


def _json_collection(meta, store, system, collection, captured, records, loc):
    if collection in OPERATIONAL_STORES:
        for i, rec in enumerate(records):
            _record_lookup(meta, store, rec, f"{loc}.records[{i}]")
        return
    rids = set()
    for i, rec in enumerate(records):
        fields = _flatten_record(rec)
        rid = fields["resource_id"]
        rids.add(rid)
        store.observations.append(interpret(
            meta, system=system, resource_id=rid, store=f"{system}:{collection}",
            locator=f"{loc}.records[{i}]", observed_at=captured, fields=fields, exists=True,
            source_kind="json"))
    store.scopes.append(Scope(meta.file_id, meta.captured_at, captured, system, f"{system}:{collection}",
                              frozenset(rids)))


def _record_lookup(meta, store, rec, loc):
    fields = dict(rec)
    system = fields.pop("system")
    rid = fields["resource_id"]
    observed = fields.get("recorded_at") or meta.captured_at
    sref = fields.get("subject_reference")
    if sref and not USER_REF_RE.match(str(sref)):
        # Lookup subject given as an email/identifier, not a stable reference.
        fields["lookup_key"] = fields.pop("subject_reference")
    store.observations.append(interpret(
        meta, system=system, resource_id=rid, store=f"record_lookup:{meta.origin}", locator=loc,
        observed_at=observed, fields=fields, source_kind="json"))


def _service_exports(meta, store, data):
    section_fields = {
        "profile_lookups": "account_service", "profiles": "analytics_platform",
        "restore_entries": "backup_service", "exports": "export_service", "connectors": "integration_service",
        "index_queries": "search_service", "audit_records": "security_service", "tickets": "support_platform",
    }
    for system, sections in data["service_exports"].items():
        for sec, items in sections.items():
            for i, item in enumerate(items):
                loc = f"service_exports.{system}.{sec}[{i}]"
                fields = dict(item)
                exists = None
                extra = []
                if sec == "profile_lookups":
                    prof = fields.pop("profile", None) or {}
                    fields.update(prof)
                    exists = fields.get("status_code") == 200
                elif sec == "index_queries":
                    hits = fields.pop("hits", {}) or {}
                    docs = [d for d in hits.get("documents", []) if d.get("resource_id") == item["resource_id"]]
                    extra.append(fields.get("query"))
                    for d in docs:
                        fields.update(d)
                    exists = bool(docs) or bool(hits.get("total"))
                else:
                    exists = True
                store.observations.append(interpret(
                    meta, system=system, resource_id=item["resource_id"], store=f"service_export:{sec}",
                    locator=loc, observed_at=item.get("observed_at") or meta.captured_at, fields=fields,
                    extra_links=[e for e in extra if e], exists=exists, source_kind="json"))


# ---------------------------------------------------------------------------
# Logs (eight observed syntaxes) and text records
# ---------------------------------------------------------------------------

KV_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)=("(?:[^"\\]|\\.)*"|\S+)')


def _kv(text: str) -> dict:
    out = {}
    for k, v in KV_RE.findall(text):
        if v.startswith('"') and v.endswith('"'):
            v = v[1:-1].replace('\\"', '"')
        out[k] = v
    return out


def parse_log_line(line: str) -> Optional[dict]:
    m = TS_RE.match(line)
    if not m:
        return None
    ts = m.group(1)
    rest = line[len(ts):].strip()
    rec: dict
    if rest.startswith("{"):
        try:
            rec = json.loads(rest)
        except json.JSONDecodeError:
            return None
        rec.setdefault("service", rec.get("logger"))
    elif rest.startswith("requestId="):
        outer = _kv(rest)
        rec = _kv(outer.get("message", ""))
        rec["service"] = outer.get("function")
        rec["trace_id"] = outer.get("requestId")
        rec["level"] = outer.get("level")
    elif rest.startswith("<"):
        sm = re.match(r"<\d+>\d+\s+(\S+)\s+(\S+)\s+(\S+)\s+-\s+-\s+(.*)$", rest)
        if not sm:
            return None
        rec = _kv(sm.group(4))
        rec["service"], rec["trace_id"] = sm.group(2), sm.group(3)
    elif rest.startswith("["):
        bm = re.match(r"\[(\w+)\]\s+\[([^\]]+)\]\s+\[([^\]]+)\]\s+([a-z_]+):\s*(.*)$", rest)
        if not bm:
            return None
        rec = _kv(bm.group(5))
        rec.update(level=bm.group(1), service=bm.group(2), trace_id=bm.group(3), event=bm.group(4))
    elif rest.startswith("|"):
        parts = [p.strip() for p in rest.strip("|").split("|", 4)]
        if len(parts) < 5:
            return None
        rec = _kv(parts[4])
        rec.update(level=parts[0], service=parts[1], trace_id=parts[2], event=parts[3])
    else:
        rec = _kv(rest)
    rec["_ts"] = ts
    return rec


LOG_SUBJECT_KEYS = ("lookup_key", "subject_key", "subject_ref", "scan_key", "owner_key", "match_key",
                    "correlation_key", "matched_value", "customer_reference", "email", "customer_email",
                    "requester_email", "owner_email")


def parse_log(meta: FileMeta, store: EvidenceStore) -> None:
    lines = meta.path.read_text(encoding="utf-8").splitlines()
    parsed: List[Tuple[int, dict]] = []
    for n, line in enumerate(lines, 1):
        rec = parse_log_line(line)
        if rec is not None:
            parsed.append((n, rec))
    groups: Dict[str, List[Tuple[int, dict]]] = defaultdict(list)
    for n, rec in parsed:
        tid = rec.get("trace_id")
        groups[tid if tid and str(tid).startswith(("tr-", "rb-")) else f"line{n}"].append((n, rec))
    for gid, items in groups.items():
        links = set()
        for _, rec in items:
            for k in LOG_SUBJECT_KEYS:
                if not _null(rec.get(k)):
                    links.add(rec[k])
        by_rid: Dict[str, List[Tuple[int, dict]]] = defaultdict(list)
        for n, rec in items:
            rid = rec.get("resource_id")
            if rid and RESOURCE_RE.fullmatch(str(rid)):
                by_rid[rid].append((n, rec))
        for rid, recs in by_rid.items():
            for n, rec in recs:
                system = rec.get("system") or rec.get("service")
                if system in ("support_replica",):
                    system = "support_platform"
                fields = {k: v for k, v in rec.items() if not k.startswith("_")}
                store_name = f"log:{rec.get('source') or system}"
                if rec.get("service") == "support_replica" or rec.get("source") == "legacy_contact_index":
                    store_name = "log:legacy_contact_index"
                ob = interpret(meta, system=system, resource_id=rid, store=store_name, locator=f"line{n}",
                               observed_at=rec["_ts"], fields=fields, extra_links=links,
                               exists=_log_exists(rec), source_kind="log")
                if rec.get("event") in NON_STATE_EVENTS:
                    ob.exists = None
                store.observations.append(ob)


NON_STATE_EVENTS = {"thumbnail_failed", "deletion_marker_check", "connection_acquired", "dependency_retry",
                    "dependency_recovered", "cache_lookup"}


def _log_exists(rec: dict) -> Optional[bool]:
    ev = rec.get("event", "")
    if ev == "record_lookup":
        return None  # handled through result
    rows = rec.get("rows")
    status = str(rec.get("http_status", ""))
    if ev.startswith("profile_lookup"):
        if status == "404" or rows == "0":
            return False
        if status == "200" or (rows and rows != "0"):
            return True
    if ev.startswith("index_query"):
        res = str(rec.get("result", "")).lower()
        if res == "miss" or rec.get("total_hits") == "0":
            return False
        if res == "hit":
            return True
    if ev.startswith("restore_policy_read"):
        return _bool(rec.get("archive_present"))
    if ev in ("audit_record_read_completed", "support_ticket_read_completed", "message_pii_scan", "message_read",
              "attachment_pii_scan", "analytics_profile_read_completed", "analytics_profile_read",
              "analytics_identity_scan", "contact_lookup_completed", "contact_query_completed",
              "connector_config_read_completed", "connector_heartbeat", "connector_config_loaded",
              "restore_candidate_evaluated", "restore_authorization_completed", "export_metadata_read_completed",
              "export_download_check_completed", "retained_order_loaded"):
        if rows == "0":
            return False
        return True
    return None


def parse_text(meta: FileMeta, store: EvidenceStore) -> None:
    text = meta.path.read_text(encoding="utf-8")
    for n, line in enumerate(text.splitlines(), 1):
        parts = [p.strip() for p in line.lstrip("- ").split("|")]
        if len(parts) == 6 and RESOURCE_RE.fullmatch(parts[2]):
            ts, system, rid, rtype, result, subject = parts
            fields = {"record_type": rtype, "result": result}
            if USER_REF_RE.match(subject):
                fields["subject_reference"] = subject
            elif "@" in subject:
                fields["lookup_key"] = subject
            store.observations.append(interpret(
                meta, system=system, resource_id=rid, store=f"record_lookup:{meta.origin}", locator=f"line{n}",
                observed_at=ts, fields=fields, source_kind="text"))


# ---------------------------------------------------------------------------
# PDFs (text layer via pdftotext)
# ---------------------------------------------------------------------------

def pdf_text(path: Path) -> Optional[str]:
    try:
        res = subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True,
                             timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return res.stdout if res.returncode == 0 else None


def parse_pdf(meta: FileMeta, store: EvidenceStore) -> None:
    text = pdf_text(meta.path)
    if text is None:
        if shutil.which("pdftotext") is None:
            meta.parse_status, meta.note = "unavailable", "pdftotext is not installed (poppler-utils)"
        else:
            meta.parse_status, meta.note = "unreadable", "pdftotext could not extract text from this file"
        return
    flat = re.sub(r"\s+", " ", text)
    emails = EMAIL_RE.findall(flat)
    phones = PHONE_TEXT_RE.findall(flat)
    users = re.findall(r"\buser_\d+\b", flat)
    rids = list(dict.fromkeys(RESOURCE_RE.findall(flat)))
    title = text.strip().splitlines()[0].strip() if text.strip() else ""
    refs = re.findall(r"\b(?:NST|CS|ORD|FIN)-[\d-]+\b", flat)
    fields = {}
    if emails:
        fields["email"] = emails[0]
    if phones:
        fields["phone"] = phones[0]
    if users:
        fields["subject_reference"] = users[0]
    retain = re.search(r"retain until (\d{1,2} \w{3} \d{4})", flat, re.I)
    if "SUPPORT" in title.upper() or "CORRECTION REQUEST" in title.upper() or re.search(r"Support reference", flat):
        category, system = "SUPPORT_ATTACHMENT", "support_platform"
    elif "COMPLIANCE JOURNAL" in title.upper() or "SIGNED RECORD" in title.upper():
        category, system = "SECURITY_AUDIT_LOG", "security_service"
        fields = {"subject_reference": users[0]} if users else {}
        if retain:
            fields["retention_until"] = _date_text(retain.group(1))
    else:
        category, system = None, None
    for rid in rids:
        if system:
            store.observations.append(interpret(
                meta, system=system, resource_id=rid, store=f"document:{title[:40]}", locator="pdf:text",
                observed_at=meta.captured_at, fields=fields, category=category, exists=True, source_kind="pdf"))
    # Reference-only documents (e.g. statements) are registered for catalog joins.
    for ref in refs:
        store.documents[ref].append({"file_id": meta.file_id, "title": title, "fields": fields,
                                     "captured_at": meta.captured_at})
    meta.note = f"title={title!r}; resources={rids}; refs={sorted(set(refs))[:5]}"


MONTHS = {m: i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct",
                                     "Nov", "Dec"], 1)}


def _date_text(s: str) -> str:
    d, mon, y = s.split()
    return f"{y}-{MONTHS[mon[:3]]:02d}-{int(d):02d}T23:59:59Z"


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def load_evidence(dataset_dir: Path, manifest: Dict[str, Dict[str, str]]) -> EvidenceStore:
    from .visual import parse_image  # local import to keep module boundaries explicit

    store = EvidenceStore()
    def natural(fid: str):
        m = re.search(r"(\d+)$", fid)
        return (fid[: m.start()] if m else fid, int(m.group(1)) if m else -1, fid)

    for fid in sorted(manifest, key=natural):
        row = manifest[fid]
        meta = FileMeta(fid, dataset_dir / row["file_path"], row["captured_at"], row["origin"])
        store.files[fid] = meta
        before = len(store.observations)
        if not meta.path.is_file():
            meta.parse_status = "missing"
            meta.note = "listed in evidence_files.csv but not present"
            continue
        suffix = meta.path.suffix.lower()
        try:
            if suffix == ".sql":
                parse_sql(meta, store)
            elif suffix == ".json":
                parse_json(meta, store)
            elif suffix == ".log":
                parse_log(meta, store)
            elif suffix == ".txt":
                parse_text(meta, store)
            elif suffix == ".pdf":
                parse_pdf(meta, store)
            elif suffix == ".png":
                parse_image(meta, store)
            else:
                meta.parse_status = "unsupported"
                meta.note = f"no parser for {suffix or 'files without an extension'}"
                continue
        except Exception as exc:  # keep going; surface in the inventory
            meta.parse_status = f"error: {exc!r}"
            continue
        meta.observations = len(store.observations) - before
        if meta.parse_status == "pending":
            meta.parse_status = "parsed"
    return store
