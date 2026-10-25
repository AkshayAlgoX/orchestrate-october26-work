"""Cached, provenance-tagged facts extracted from the PNG evidence.

No OCR engine is installed in the development environment (no tesseract), so
the 11 screenshots were interpreted visually once by the coding model
(Claude Code, model claude-opus-5-5) during development on 2026-10-24 and
transcribed here as structured facts. Each entry is bound to the SHA-256 of
the image bytes: if an image changes, its facts are not used and the file is
reported as `stale_visual_cache`, never silently trusted.

Only facts visibly present in the screenshot are recorded. Facts that a
screenshot does not show (for example an owner that is not displayed) are
left out; such links can only come from other evidence and are labelled as
claims there. The full-dataset run makes zero model calls: this cache is the
only image-derived input.

Each fact: system, resource_id (or None when the screen shows only a document
reference), doc_ref (a visible reference usable for catalog joins), fields as
displayed, exists, locator (screen region), confidence.
"""

from __future__ import annotations

import hashlib

from .model import Observation

PROVENANCE = "visual transcription by claude-opus-5-5 (Claude Code), 2026-10-24; no OCR engine available"

VISUAL_FACTS = {
    # account_admin_console_20260926.png
    "d5c32ca5b6e1a2b4bbe9f5d41835ef1fd5d4f7061dab9067a4f9f88cea3c1861": {
        "observed_at": None,
        "facts": [
            {"system": "account_service", "resource_id": "account_5762", "exists": True,
             "fields": {"full_name": "Haruka Sato", "customer_reference": "user_11",
                        "email": "haruka.s1838@zoho.com", "phone": "+81 90 7610 2507"},
             "locator": "header + Email/Phone cards", "confidence": "high"},
            # Customer details panel lists "Support  CS-8538 · support_9538" on this profile and the
            # Activity timeline shows "28 Aug support_9538 created". The ticket content is not shown.
            {"system": "support_platform", "resource_id": "support_9538", "exists": True,
             "fields": {"customer_reference": "user_11", "ticket_id": "CS-8538"},
             "locator": "Customer details > Support row; Activity '28 Aug support_9538 created'",
             "confidence": "high", "reference_only": True},
        ],
    },
    # search_index_inspector_20260901.png
    "75afdb0ae53a4510c83399a70d88073cb4fb2747454095756e4ddad1a8c70605": {
        "observed_at": "2026-09-01T06:00:14Z",
        "facts": [
            {"system": "search_service", "resource_id": "search_428", "exists": True,
             "fields": {"display_name": "Gabriel Silva", "email": "g.silva76@gmail.com",
                        "customer_reference": "user_13", "index_name": "customer_directory_v3"},
             "locator": "Results row 1 + Raw document panel", "confidence": "high"},
            {"system": "search_service", "resource_id": "search_1874", "exists": True,
             "fields": {"email": "g.silva76.jr@gmail.com"},
             "locator": "Results row 2", "confidence": "high"},
        ],
    },
    # connector_management_console_20260807.png
    "e81fabc93ea1b5f4c918d934cdb2bea05d61737adfcc29f281494f5d8ec46355": {
        "observed_at": "2026-08-07T08:00:00Z",
        "facts": [
            {"system": "integration_service", "resource_id": "data_flow_1121", "exists": True,
             "fields": {"subject_selector": "jain.aditi@gmail.com", "enabled": True,
                        "destination": "customer_analytics_warehouse", "mode": "continuous_sync"},
             "locator": "Topology row 1 + detail panel (Status Healthy, Continuous sync)", "confidence": "high"},
            {"system": "integration_service", "resource_id": "data_flow_9846", "exists": True,
             "fields": {"subject_selector": "lucia-torres@gmail.com", "enabled": True,
                        "destination": "customer_analytics_warehouse", "mode": "continuous_sync"},
             "locator": "Topology row 2 (Continuous, 842 events/hour)", "confidence": "high"},
        ],
    },
    # analytics_profile_view_20260713.png
    "4e4cb259bc2010b622b0c53fe5872258f994fefc23452a83bb27473758693484": {
        "observed_at": None,
        "facts": [
            {"system": "analytics_platform", "resource_id": "analytics_7438", "exists": True,
             "fields": {"full_name": "Omar Darwish", "user_id": "user_20", "email": "darwish.omar@icloud.com"},
             "locator": "page header subtitle", "confidence": "high"},
        ],
    },
    # customer_export_console_20260618.png
    "18b95c1bd8b8956e40216df935498a9f3c181ea2a69b5cd6d3c0841a816d5557": {
        "observed_at": None,
        "facts": [
            {"system": "export_service", "resource_id": "export_1260", "exists": True,
             "fields": {"owner_email": "ingrid.dahl@outlook.com", "status": "Ready",
                        "expires_at": "2026-06-25T06:42:00Z"},
             "locator": "All exports row 2 + detail panel", "confidence": "high"},
            {"system": "export_service", "resource_id": "export_1261", "exists": True,
             "fields": {"owner_email": "marcus.lee@northstar.shop"}, "locator": "row 1", "confidence": "high"},
            {"system": "export_service", "resource_id": "export_1259", "exists": True,
             "fields": {"owner_email": "sara.kim@northstar.shop"}, "locator": "row 3", "confidence": "high"},
            {"system": "export_service", "resource_id": "export_1258", "exists": True,
             "fields": {"owner_email": "daniel.park@northstar.shop"}, "locator": "row 4", "confidence": "high"},
        ],
    },
    # support_ticket_console_20260524.png
    "542b7aec17e6b2f38827896e7eb52c70e4bfaaca22433621be1f80f54884e944": {
        "observed_at": None,
        "facts": [
            {"system": "support_platform", "resource_id": "support_1034", "exists": True,
             "category": "SUPPORT_MESSAGE",
             "fields": {"requester_name": "Kofi Agyeman", "requester_email": "kagyeman8947@fastmail.com",
                        "ticket_id": "CS-5034", "assigned_agent": "Maya Chen", "order": "ORD-20265034"},
             "locator": "ticket header, Customer/Email cards, conversation with agent and order panel",
             "confidence": "high"},
        ],
    },
    # backup_restore_console_20260429.png
    "2c246aa5843bec609b9ddfdf1154cd81e0fd61996f83954735e9856e48c18bf3": {
        "observed_at": None,
        "facts": [
            # The screen does not state whether restore is blocked for this archive.
            {"system": "backup_service", "resource_id": "backup_9287", "exists": True,
             "fields": {"customer_email": "areed1433@gmail.com",
                        "archive_key": "s3://northstar-backups/customer-platform/weekly/segment-42-20260426.tar.zst",
                        "retention_end": "2026-07-26"},
             "locator": "Archives row 3 + detail panel", "confidence": "high"},
            {"system": "backup_service", "resource_id": "backup_9291", "exists": True,
             "fields": {"customer_email": "bluepeakco@outlook.com"}, "locator": "row 1", "confidence": "high"},
            {"system": "backup_service", "resource_id": "backup_9288", "exists": True,
             "fields": {"customer_email": "miller.logistics@company.com"}, "locator": "row 2", "confidence": "high"},
            {"system": "backup_service", "resource_id": "backup_9286", "exists": True,
             "fields": {"customer_email": "summit-retail@company.com"}, "locator": "row 4", "confidence": "high"},
        ],
    },
    # security_audit_explorer_20260905.png
    "ca59f693f4e7002466b3fdeaf1bb6763618770963dd811b60003772d07b4b03b": {
        "observed_at": None,
        "facts": [
            {"system": "security_service", "resource_id": f"audit_record_00{i}", "exists": True,
             "fields": {"subject_reference": "user_03", "integrity": "Valid"},
             "locator": f"Events table, reference audit_record_00{i}", "confidence": "high"}
            for i in range(1, 7)
        ],
    },
    # deleted_account_reference_20260310.png  (no resource_id shown; only the retained key)
    "68c28a72afd6ed184d5990d15c543f8a516face204f0360f732755a8a63c28be": {
        "observed_at": "2026-03-10T07:18:00Z",
        "facts": [
            {"system": "identity_service", "resource_id": None, "doc_ref": "user_17", "exists": True,
             "category": "ACCOUNT_TOMBSTONE",
             "fields": {"subject_reference": "user_17", "status": "Retained reference"},
             "locator": "page header 'user_17' + 'Retained reference' badge; no name/email/phone displayed",
             "confidence": "high"},
        ],
    },
    # object_storage_browser_20260213.png  (no owner shown)
    "96e9a191380c29b9e0ab997007268c4cd6353cb6721ebd1ed4a32599d8d37e04": {
        "observed_at": "2026-02-13T08:00:00Z",
        "facts": [
            {"system": "object_storage", "resource_id": None, "doc_ref": "delivery_confirmation_19.pdf",
             "exists": True, "category": "SUPPORT_ATTACHMENT",
             "fields": {"bucket": "northstar-customer-data", "prefix": "eu-west-1/support-attachments/2026/02",
                        "uploader": "support-service"},
             "locator": "Objects row 1 + detail panel", "confidence": "high"},
        ],
    },
    # marketing_profile_console.png
    "51fa0e6a38f8fa3629ba1b0d0b90b4ed139715956b97bfd09a6f1a6232c15df5": {
        "observed_at": "2026-09-15T23:40:00Z",
        "facts": [
            {"system": "marketing_platform", "resource_id": "marketing_001", "exists": True,
             "fields": {"full_name": "Soo-jin Park", "email": "sj.park@naver.com", "phone": "+82 10-5837-9451"},
             "locator": "profile header", "confidence": "high"},
        ],
    },
}


def image_provenance(path) -> dict:
    """Integrity facts about an image file. This is NOT text recognition.

    The SHA-256 binds cached facts to the exact bytes; when Pillow is installed the file
    is also decoded to confirm it is a readable image and to record format and size.
    """
    data = path.read_bytes()
    info = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "decoder": "unavailable"}
    try:
        from PIL import Image  # optional dependency; never required for a run
    except ImportError:
        return info
    try:
        with Image.open(path) as im:
            im.verify()                      # structural integrity check
        with Image.open(path) as im:
            info.update(decoder="Pillow", format=im.format, width=im.width, height=im.height, mode=im.mode,
                        readable=True)
    except Exception as exc:                 # unreadable/corrupt image: surfaced, never trusted
        info.update(decoder="Pillow", readable=False, error=repr(exc))
    return info


def parse_image(meta, store) -> None:
    from .evidence import interpret  # avoid import cycle at module load

    meta.image = image_provenance(meta.path)
    digest = meta.image["sha256"]
    if meta.image.get("readable") is False:
        meta.parse_status = "unreadable_image"
        meta.note = f"image failed to decode: {meta.image.get('error')}"
        return
    entry = VISUAL_FACTS.get(digest)
    if entry is None:
        meta.parse_status = "uncovered_image"
        meta.note = "no verified visual transcription for this image content"
        return
    meta.parse_status = "parsed_visual_cache"
    dims = (f"{meta.image.get('format')} {meta.image.get('width')}x{meta.image.get('height')}"
            if meta.image.get("readable") else "decode check unavailable (Pillow not installed)")
    meta.note = f"{PROVENANCE}; sha256 verified; {dims}"
    for fact in entry["facts"]:
        fields = dict(fact["fields"])
        if fact.get("resource_id"):
            ob = interpret(meta, system=fact["system"], resource_id=fact["resource_id"],
                           store=f"screen:{fact['system']}", locator=f"image:{fact['locator']}",
                           observed_at=entry["observed_at"] or meta.captured_at, fields=fields,
                           category=fact.get("category"), exists=fact["exists"], source_kind="image")
            if fact.get("reference_only"):
                # The screen proves the record exists and whose it is, but not its type or
                # content: no category and no exposure judgement come from this observation.
                ob.category = None
            store.observations.append(ob)
        if fact.get("doc_ref"):
            store.documents[fact["doc_ref"]].append({
                "file_id": meta.file_id, "title": f"screen:{fact['system']}", "fields": fields,
                "captured_at": meta.captured_at, "system": fact["system"], "category": fact.get("category"),
                "observed_at": entry["observed_at"] or meta.captured_at, "source_kind": "image",
                "locator": f"image:{fact['locator']}"})
