"""Configuration loading, and accuracy of the prompt/instruction documents against the code.

Run:  python3 code/tests/test_config_prompts.py [--dataset /path/to/dataset]

The documents under code/prompts/ describe deterministic components (no model is called at run
time). Each one names things that exist in the code or the dataset: tool contracts, classifier
behaviour on quoted request texts, and the screenshots behind the visual cache. These tests check
the tables (tool table, intent examples, image inventory) against the code and data, and reject any
"you are ..." persona text. Prose outside the tables is not machine-checked.
"""

import csv
import hashlib
import json
import os
import re
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
CODE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_pipeline as tp  # noqa: E402
from ghost_audit.agent import describe_tools  # noqa: E402
from ghost_audit.audit import Config  # noqa: E402
from ghost_audit.intent import classify  # noqa: E402
from ghost_audit.visual import VISUAL_FACTS  # noqa: E402
from main import ROOT  # noqa: E402

PROMPTS = ROOT / "code" / "prompts"
SHA = re.compile(r"\b[0-9a-f]{64}\b")
RID = re.compile(r"`([a-z_]+_\d+|[a-z0-9_]+\.pdf)`")


def table_rows(text, first_cell):
    """Cells of markdown table rows whose first cell matches `first_cell` (a regex)."""
    rows = []
    for line in text.splitlines():
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if cells and re.fullmatch(first_cell, cells[0]):
                rows.append(cells)
    return rows


class TestConfigAndPrompts(unittest.TestCase):
    def test_audit_config_defaults_match_code_defaults(self):
        config_path = ROOT / "code" / "config" / "audit_config.json"
        self.assertTrue(config_path.is_file(), f"Missing config file {config_path}")
        loaded = Config.load(config_path)
        default = Config()
        self.assertEqual(loaded.window, default.window)
        self.assertEqual(loaded.workflow_links, default.workflow_links)
        self.assertEqual(loaded.proximity_margin_min, default.proximity_margin_min)
        self.assertEqual(loaded.identity_lookback, default.identity_lookback)
        self.assertEqual(loaded.ambiguous, default.ambiguous)
        self.assertEqual(loaded.post_cutoff_retention, default.post_cutoff_retention)
        self.assertEqual(loaded.activity_only_categories, default.activity_only_categories)
        self.assertEqual(loaded.relaxed_colocation, default.relaxed_colocation)
        self.assertEqual(loaded.reference_links, default.reference_links)

    def test_prompts_files_exist_and_substantive(self):
        self.assertTrue(PROMPTS.is_dir(), f"Missing prompts directory {PROMPTS}")
        for name in ("visual_transcription.md", "intent_classification.md", "agent_system_prompt.md"):
            p = PROMPTS / name
            self.assertTrue(p.is_file(), f"Missing prompt file {p}")
            self.assertGreater(len(p.read_text(encoding="utf-8")), 200, f"Prompt file {p} is too short or empty")


class PromptDocumentAccuracy(unittest.TestCase):
    def read(self, name):
        return (PROMPTS / name).read_text(encoding="utf-8")

    def test_protocol_tool_table_is_the_enforced_registry(self):
        rows = table_rows(self.read("agent_system_prompt.md"), r"`[a-z_]+`")
        documented = [(r[0].strip("`"), r[1], "" if r[2] == "none" else r[2].strip("`")) for r in rows]
        registry = [(t["name"], t["phase"], t.get("precondition_id", "")) for t in describe_tools()]
        self.assertEqual(documented, registry)

    def test_intent_examples_are_what_the_classifier_returns(self):
        rows = table_rows(self.read("intent_classification.md"), r"`case_\d+`")
        self.assertGreaterEqual(len(rows), 6)
        labels = {r[1] for r in rows}
        self.assertEqual(labels, {"deletion", "not deletion"})
        for cid, label, text in ((r[0].strip("`"), r[1], r[2]) for r in rows):
            self.assertEqual(classify(text).authorizes_deletion, label == "deletion", f"{cid}: {text}")

    def test_intent_examples_quote_the_participant_requests(self):
        tp.require_dataset(("deletion_requests.csv", "sample_deletion_requests.csv"))
        texts = {}
        for name in ("deletion_requests.csv", "sample_deletion_requests.csv"):
            with open(tp.DATASET / name, encoding="utf-8", newline="") as fh:
                texts.update({r["case_id"]: r["request_text"] for r in csv.DictReader(fh)})
        for r in table_rows(self.read("intent_classification.md"), r"`case_\d+`"):
            self.assertEqual(texts.get(r[0].strip("`")), r[2], r[0])

    def test_visual_inventory_is_the_cache(self):
        rows = table_rows(self.read("visual_transcription.md"), r"`[a-z0-9_]+\.png`")
        by_sha = {SHA.search(r[1]).group(0): r for r in rows}
        self.assertEqual(len(by_sha), len(rows), "an image is listed twice")
        self.assertEqual(set(by_sha), set(VISUAL_FACTS))
        for sha, row in by_sha.items():
            cached = {f.get("resource_id") or f.get("doc_ref") for f in VISUAL_FACTS[sha]["facts"]}
            self.assertEqual(set(RID.findall(row[2])), cached, row[0])

    def test_visual_inventory_names_the_dataset_images(self):
        tp.require_dataset(("evidence/",))
        images = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in sorted((tp.DATASET / "evidence" / "images").glob("*.png"))}
        rows = table_rows(self.read("visual_transcription.md"), r"`[a-z0-9_]+\.png`")
        self.assertEqual({r[0].strip("`"): SHA.search(r[1]).group(0) for r in rows}, images)

    def test_no_document_presents_a_model_prompt(self):
        """Zero model calls at run time (usage_report.md): no document may present a prompt as sent to one."""
        for p in sorted(PROMPTS.glob("*.md")):
            self.assertNotRegex(p.read_text(encoding="utf-8"), r"(?i)\byou are\b", p.name)


if __name__ == "__main__":
    argv = list(sys.argv)
    if "--dataset" in argv:
        i = argv.index("--dataset")
        tp.DATASET = Path(argv[i + 1]).resolve()
        os.environ["GHOST_AUDIT_DATASET"] = str(tp.DATASET)
        del argv[i:i + 2]
    unittest.main(verbosity=2, argv=argv)
