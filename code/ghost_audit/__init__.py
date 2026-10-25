"""Deterministic deletion-audit pipeline for "Ghost Data Still Exists".

Modules:
  model      - shared dataclasses and vocabulary
  loaders    - CSV inputs, identity index, activity stream
  evidence   - per-format evidence parsers producing typed observations
  visual     - cached, provenance-tagged facts extracted from PNG evidence
  intent     - deletion-authorization classifier for request_text
  linking    - subject <-> resource attribution
  policy     - deletion_rules.csv categories and required treatments
  planner    - findings / remediation / verification construction
  output     - ordering, serialization and contract self-checks
"""
