# Screenshot transcription: method, rules and inventory

**No model is called at run time.** The 11 PNG screenshots under `dataset/evidence/images/` were
read once during development. The facts from them are a cache in `code/ghost_audit/visual.py`, and
that cache is the only image-derived input to the pipeline.

## How the transcription was actually done

- **When and by whom:** on 2026-10-24, around 13:16 UTC. The coding assistant (Claude Code, model
  `claude-opus-5-5`) opened each PNG with its file-viewing tool in the development session recorded
  in `log.txt` and in the agent trace.
- **No separate prompt was used, so no verbatim prompt exists.** There was no OCR engine (no
  tesseract) and no model API. The assistant read each image within the ordinary development
  conversation. The rules below are reconstructed from `visual.py` and describe how the cache was
  written. They are not a prompt that was sent to a model.
- **Cross-check:** the emails read from the screens were looked up with plain text search (`grep`)
  in `user_identities.csv`, `user_audit_log.csv` and the two request files. This happened during the
  viewing (13:16 to 13:17 UTC), before `visual.py` was written (14:00 UTC).
- **Cost:** those development tokens were not metered by the pipeline, so `usage_report.md` does
  not count them. It says so.

## Rules the cache follows

1. Record only what the screen shows. A field that is not displayed is left out. An owner that is
   not displayed is never inferred.
2. If a screen shows only a cross-system reference (for example a support row on an account page),
   the record is marked `reference_only`. It proves that the record exists and whose it is. No
   category comes from it. Its displayed fields (a ticket id and a user reference, no direct
   identifiers) still take part in the state calculation, where they show no exposed PII.
3. If no resource id is displayed, `resource_id` is `None`, and the visible key is kept as a
   `doc_ref` for catalog joins: the retained `user_17` reference, and the object
   `delivery_confirmation_19.pdf`.
4. Each fact carries a screen locator and a confidence.
5. Each image's facts are keyed by the SHA-256 of its bytes. An image whose bytes differ is
   reported as `uncovered_image` and contributes nothing. It is never silently trusted.

## Inventory

`code/tests/test_config_prompts.py` fails unless this table lists exactly the cache's images,
hashes and resources, and (with the dataset) exactly the PNG files in `dataset/evidence/images/`.

| Image | SHA-256 | Resources (or `doc_ref`) | What the screen shows |
|---|---|---|---|
| `account_admin_console_20260926.png` | d5c32ca5b6e1a2b4bbe9f5d41835ef1fd5d4f7061dab9067a4f9f88cea3c1861 | `account_5762`, `support_9538` | Account profile with contact cards; the support ticket appears only as a reference on the profile |
| `analytics_profile_view_20260713.png` | 4e4cb259bc2010b622b0c53fe5872258f994fefc23452a83bb27473758693484 | `analytics_7438` | Analytics profile whose header shows a name, user reference and email |
| `backup_restore_console_20260429.png` | 2c246aa5843bec609b9ddfdf1154cd81e0fd61996f83954735e9856e48c18bf3 | `backup_9287`, `backup_9291`, `backup_9288`, `backup_9286` | Backup archive list with one detail panel; the screen does not say whether restore is blocked |
| `connector_management_console_20260807.png` | e81fabc93ea1b5f4c918d934cdb2bea05d61737adfcc29f281494f5d8ec46355 | `data_flow_1121`, `data_flow_9846` | Two enabled continuous-sync connectors with subject selectors |
| `customer_export_console_20260618.png` | 18b95c1bd8b8956e40216df935498a9f3c181ea2a69b5cd6d3c0841a816d5557 | `export_1260`, `export_1261`, `export_1259`, `export_1258` | Export list with owner emails; one export open in the detail panel |
| `deleted_account_reference_20260310.png` | 68c28a72afd6ed184d5990d15c543f8a516face204f0360f732755a8a63c28be | `user_17` | Retained account reference with no name, email or phone displayed (tombstone) |
| `marketing_profile_console.png` | 51fa0e6a38f8fa3629ba1b0d0b90b4ed139715956b97bfd09a6f1a6232c15df5 | `marketing_001` | Marketing profile header with name, email and phone |
| `object_storage_browser_20260213.png` | 96e9a191380c29b9e0ab997007268c4cd6353cb6721ebd1ed4a32599d8d37e04 | `delivery_confirmation_19.pdf` | Support-attachment object in a storage bucket; no owner shown |
| `search_index_inspector_20260901.png` | 75afdb0ae53a4510c83399a70d88073cb4fb2747454095756e4ddad1a8c70605 | `search_428`, `search_1874` | Two search documents, one with a raw-document panel; the second is a lookalike email |
| `security_audit_explorer_20260905.png` | ca59f693f4e7002466b3fdeaf1bb6763618770963dd811b60003772d07b4b03b | `audit_record_001`, `audit_record_002`, `audit_record_003`, `audit_record_004`, `audit_record_005`, `audit_record_006` | Six security audit events for one subject reference, integrity "Valid" |
| `support_ticket_console_20260524.png` | 542b7aec17e6b2f38827896e7eb52c70e4bfaaca22433621be1f80f54884e944 | `support_1034` | Support ticket with requester name and email, assigned agent and order panel |
