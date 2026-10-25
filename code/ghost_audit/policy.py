"""deletion_rules.csv -> required treatment per data category.

Categories come from the record itself (explicit record_type, collection or
event semantics; see evidence.interpret). The rule table is read from the
dataset, not hard-coded, so a changed rule changes the plan.
"""

from __future__ import annotations

from typing import Dict, List

# Processes are not data categories in deletion_rules.csv; their obligation
# comes from the completion definition ("no active sync, import, or restore
# path can recreate the data").
PROCESS_CATEGORIES = {"DATA_FLOW"}


class Policy:
    def __init__(self, rules: List[Dict[str, str]]):
        self.rules = {r["data_category"]: r for r in rules}

    def action(self, category: str) -> str:
        if category in PROCESS_CATEGORIES:
            return "DISABLE"
        rule = self.rules.get(category)
        return rule["required_action"].upper() if rule else "UNKNOWN"

    def rule_id(self, category: str) -> str:
        rule = self.rules.get(category)
        return rule["rule_id"] if rule else ""

    def condition(self, category: str) -> str:
        rule = self.rules.get(category)
        return rule["condition"] if rule else ""
