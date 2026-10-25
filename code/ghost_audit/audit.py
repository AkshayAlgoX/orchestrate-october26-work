"""Pipeline orchestration: dataset + evidence -> one audited row per request."""

from __future__ import annotations

from collections import defaultdict
import dataclasses
from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .evidence import SYSTEM_CATEGORY, EvidenceStore, interpret, load_evidence, FileMeta
from .intent import classify
from .linking import Link, catalog_claims, catalog_links, direct_links, eligible, resolve_workflow, ts, \
    workflow_candidates
from .loaders import Dataset, load_dataset, read_csv
from .model import Observation
from .output import to_json
from .planner import ResourceState, plan_case, resolve_state
from .policy import Policy


@dataclass
class Config:
    # Lower bound on evidence capture time for a case: "requested_at" or "none".
    window: str = "requested_at"
    workflow_links: bool = True
    # Minutes by which the nearest case must beat the runner-up to break a tie
    # between cases competing for the same identifier-less resource (None = off).
    proximity_margin_min: Optional[float] = 5.0
    # Allow evidence captured before requested_at (but <= audit_at) to associate a
    # resource with the subject; state still comes from the request window.
    identity_lookback: bool = False
    # What to do with identifier-less resources that several requests explain
    # equally well: "drop" (precision) or "all" (recall).
    ambiguous: str = "drop"
    # EXPERIMENT (off by default): for INCOMPLETE plans only, add a future
    # VERIFY_RECORD_PRESENT for a policy-retained record of the subject that is
    # visible only in a file captured after audit_at but whose record time is
    # <= audit_at. Never cited, never used for state or status.
    post_cutoff_retention: bool = False
    # Categories for which resources known only from deletion-worker activity
    # (never mentioned in any evidence file) may be attributed (empty = off).
    activity_only_categories: tuple = ("USER_PROFILE",)
    # A request with no directly linked evidence file may receive an identifier-less
    # observed resource from any in-window file, still subject to global uniqueness
    # (sprint 1, experiment X1: +1 correct sample step, 0 false positives, 0 main rows changed).
    relaxed_colocation: bool = True
    # Sprint 4 (X-REF): follow explicit record-to-record references from subject-linked records.
    reference_links: bool = True

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Config:
        valid_fields = {f.name for f in dataclasses.fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in valid_fields}
        if "activity_only_categories" in kwargs and isinstance(kwargs["activity_only_categories"], list):
            kwargs["activity_only_categories"] = tuple(kwargs["activity_only_categories"])
        return cls(**kwargs)

    @classmethod
    def load(cls, path: str | Path) -> Config:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_dict(data)


@dataclass
class CaseResult:
    case_id: str
    status: str
    steps_json: str
    trace: Dict = field(default_factory=dict)


class Auditor:
    def __init__(self, dataset: Dataset, evidence: EvidenceStore, config: Optional[Config] = None):
        self.ds = dataset
        self.ev = evidence
        self.cfg = config or Config()
        self.policy = Policy(dataset.rules)
        self.by_resource = evidence.by_resource()
        self.claims = catalog_claims(dataset.activity)
        self.activity_by_resource: Dict[str, List[dict]] = defaultdict(list)
        for a in dataset.activity:
            self.activity_by_resource[a["resource_id"]].append(a)
        # Resources whose observations carry another identity's tokens, per user.
        self.resource_owners: Dict[str, Set[tuple]] = defaultdict(set)
        for ob in evidence.observations:
            for uid in dataset.identities.owners_of(ob.link_values):
                self.resource_owners[ob.resource_id].add((uid, ob.file_captured_at))
        anon = {a for a in self.policy.rules if self.policy.action(a) == "ANONYMIZE"}
        red = {a for a in self.policy.rules if self.policy.action(a) == "REDACT"}
        self.anonymize_categories, self.redact_categories = anon, red
        self.retain_categories = {a for a in self.policy.rules if self.policy.action(a) == "RETAIN"} - {"BACKUP_ARCHIVE"}

    @staticmethod
    def _key(req):
        return (req["case_id"], req["email"].strip().lower(), req["requested_at"], req["reported_at"], req["audit_at"])

    def _lower(self, req):
        return req["requested_at"] if self.cfg.window == "requested_at" else None

    def _content_links(self, req, ident):
        """Direct and catalog links (content-grounded) plus document-derived observations."""
        audit_at, lower = req["audit_at"], self._lower(req)
        # Identity association may use any evidence captured up to audit_at (a prior
        # relationship); the audited state itself only uses the request window.
        link_lower = None if self.cfg.identity_lookback else lower
        links: Dict[str, Link] = direct_links(ident, self.ev.observations, link_lower, audit_at,
                                              self.ds.identities)
        extra_obs: Dict[str, List[Observation]] = defaultdict(list)
        for rid, info in catalog_links(ident, self.claims, self.by_resource, self.ev.documents, lower, audit_at,
                                       self.ds.identities).items():
            if rid not in links:
                links[rid] = info["link"]
            extra_obs[rid] += self.document_observations(rid, info)
        if self.cfg.reference_links:
            self._reference_links(req, ident, links, lower, audit_at)
        return links, extra_obs

    def document_observations(self, rid: str, info: dict) -> List[Observation]:
        """Observations derived from the documents behind an accepted catalog claim."""
        claim, out = info["claim"], []
        for d in info["docs"]:
            meta = self.ev.files[d["file_id"]]
            category = d.get("category") or ("SUPPORT_ATTACHMENT" if claim.exclusive else None)
            out.append(interpret(
                meta, system=claim.target_system, resource_id=rid, store=f"document:{claim.ref}",
                locator=d.get("locator", "document"), observed_at=d.get("observed_at") or meta.captured_at,
                fields=d["fields"], category=category, exists=True, source_kind=d.get("source_kind", "pdf")))
        return out

    def references_from(self, rid: str, lower: Optional[str], audit_at: str) -> List[tuple]:
        """(referenced resource, file_id) pairs named by eligible observations of `rid`."""
        return [(ref, ob.file_id) for ob in self.by_resource.get(rid, []) if eligible(ob, lower, audit_at)
                for ref in ob.references]

    def reference_system(self, user_id: str, ref: str, audit_at: str) -> Optional[str]:
        """System of an explicitly referenced resource, or None when evidence available at
        audit_at attributes it to another person or nothing establishes its system. The
        system comes from the resource's own observations or deletion-workflow activity on
        it, never from the identifier's prefix."""
        if any(u != user_id and cap <= audit_at for u, cap in self.resource_owners.get(ref, ())):
            return None
        systems = sorted({o.system for o in self.by_resource.get(ref, [])
                          if o.file_captured_at <= audit_at and o.system})
        systems += sorted({a["target_system"] for a in self.activity_by_resource.get(ref, [])
                           if a["source_system"] == "deletion_worker" and a["event_time"] <= audit_at})
        return systems[0] if systems else None

    def _reference_links(self, req, ident, links, lower, audit_at):
        """Resources explicitly named by a content-linked record, followed to closure.

        Example: a search document whose source_resource_id names the account it was
        built from. Each wave follows the references of the resources added by the
        previous wave, in resource order. Workflow-attributed resources are never
        expanded: that attribution is an inference, and chaining inferences is not proof.
        """
        frontier = sorted(links)
        while frontier:
            added = []
            for rid in frontier:
                for ref, fid in self.references_from(rid, lower, audit_at):
                    if ref in links:
                        continue
                    system = self.reference_system(ident.user_id, ref, audit_at)
                    if system:
                        links[ref] = Link(ref, system, "reference", [fid])
                        added.append(ref)
            frontier = sorted(added)

    def prepare(self, universe: List[Dict[str, str]]) -> None:
        """Content links for every open deletion request, then global workflow assignment.

        All participant-visible requests compete for identifier-less resources,
        so a resource is never given to one case when another request explains
        it equally well.
        """
        self.content: Dict[tuple, tuple] = {}
        self.prepared_key: Dict[str, tuple] = {}
        cands, direct_cats, anchors = {}, {}, {}
        prepared = []
        for req in universe:
            if not classify(req["request_text"]).authorizes_deletion:
                continue
            uid = self.ds.identities.resolve_email(req["email"])
            if uid is None:
                continue
            ident = self.ds.identities.by_user[uid]
            links, extra = self._content_links(req, ident)
            self.content[self._key(req)] = (uid, links, extra)
            self.prepared_key[req["case_id"]] = self._key(req)
            prepared.append((req, uid, links))
        # Resources that a request's own content evidence attributes to it (direct,
        # catalog or explicit reference), with the capture time of that evidence. A
        # different request may not take such a resource through workflow inference
        # when the claim was available at its own audit cutoff.
        claims: Dict[str, List[tuple]] = defaultdict(list)
        for req, uid, links in prepared:
            for rid, link in links.items():
                caps = [self.ev.files[f].captured_at for f in link.basis if f in self.ev.files]
                if caps:
                    claims[rid].append((req["case_id"], min(caps)))
        for req, uid, links in prepared:
            if not self.cfg.workflow_links:
                continue
            owned = {rid for rid, owners in self.resource_owners.items()
                     if any(u != uid and cap <= req["audit_at"] for u, cap in owners)}
            owned |= {rid for rid, cl in claims.items()
                      if any(c != req["case_id"] and cap <= req["audit_at"] for c, cap in cl)}
            cands[req["case_id"]] = workflow_candidates(req, links, self.by_resource, self.activity_by_resource,
                                                        self._lower(req), req["audit_at"], owned,
                                                        activity_only=set(self.cfg.activity_only_categories),
                                                        relaxed_colocation=self.cfg.relaxed_colocation)
            cats = set()
            for rid, link in links.items():
                obs = self.by_resource.get(rid, [])
                cs = [o.category for o in obs if o.category]
                if cs:
                    cats.add(max(sorted(set(cs)), key=cs.count))
                elif SYSTEM_CATEGORY.get(link.system):
                    cats.add(SYSTEM_CATEGORY[link.system])
            direct_cats[req["case_id"]] = cats
            rep = ts(req["reported_at"])
            anchors[req["case_id"]] = [ts(a["event_time"]) for rid in links
                                       for a in self.activity_by_resource.get(rid, [])
                                       if a["source_system"] == "deletion_worker"
                                       and abs((ts(a["event_time"]) - rep).total_seconds()) <= 3 * 3600]
        self.workflow = resolve_workflow(cands, direct_cats, anchors, self.cfg.proximity_margin_min,
                                         self.cfg.ambiguous) \
            if self.cfg.workflow_links else {}
        self.candidates = cands

    def audit(self, req: Dict[str, str]) -> CaseResult:
        cid = req["case_id"]
        intent = classify(req["request_text"])
        trace = {"intent": intent.reason}
        if not intent.authorizes_deletion:
            return CaseResult(cid, "NOT_APPLICABLE", "[]", trace)
        uid = self.ds.identities.resolve_email(req["email"])
        trace["subject"] = uid
        if uid is None:
            trace["error"] = "request email does not resolve to a unique identity"
            return CaseResult(cid, "INCOMPLETE", "[]", trace)
        audit_at = req["audit_at"]
        if self._key(req) not in self.content:
            self.content[self._key(req)] = (uid,) + self._content_links(req, self.ds.identities.by_user[uid])
        _, links, extra_obs = self.content[self._key(req)]
        links = dict(links)
        same_request = getattr(self, "prepared_key", {}).get(cid) == self._key(req)
        for rid, link in (getattr(self, "workflow", {}).get(cid, {}) if same_request else {}).items():
            links.setdefault(rid, link)
        cand_map = getattr(self, "candidates", {}).get(cid, {}) if same_request else {}
        unresolved = sorted(r for r in cand_map if r not in links)

        states = [self.resource_state(req, rid, link, extra_obs, cand_map.get(rid, {}))
                  for rid, link in sorted(links.items())]
        status, steps, notes = plan_case(states, self.policy, audit_at)
        if status == "INCOMPLETE" and self.cfg.post_cutoff_retention:
            steps += self._post_cutoff_retention(req, uid, states)
        trace["links"] = {rid: (l.how, l.basis) for rid, l in links.items()}
        trace["unresolved_workflow_candidates"] = unresolved
        trace["states"] = {s.resource_id: (s.category, s.exists, s.exposed, s.flow_enabled, s.restore_allowed,
                                           s.evidence_id, s.note) for s in states}
        trace["notes"] = notes
        # Coverage of obligations every deletion normally carries (diagnostic only;
        # nothing is added to the plan without evidence).
        cats = {s.category for s in states}
        trace["coverage"] = {
            "account": "established" if "USER_PROFILE" in cats else
                       (f"ambiguous({sum(1 for r in unresolved if cand_map.get(r, {}).get('category') == 'USER_PROFILE')})"
                        if any(cand_map.get(r, {}).get("category") == "USER_PROFILE" for r in unresolved) else "none"),
            "retained_security": "established" if "SECURITY_AUDIT_LOG" in cats else "none",
        }
        if not steps:
            # COMPLETE requires proof; with nothing admissible there is neither proof of
            # compliance nor a finding. The row stays COMPLETE (the only contract-valid
            # status without a finding) but is flagged so it is never silently accepted.
            trace["insufficient_evidence"] = True
            trace["error"] = "no evidence-backed resources found"
        return CaseResult(cid, status, to_json(steps), trace)


    def resource_state(self, req, rid: str, link: Link, extra_obs, winfo: dict) -> ResourceState:
        """State of one linked resource at audit_at from admissible observations."""
        audit_at, lower = req["audit_at"], self._lower(req)
        obs = self.by_resource.get(rid, []) + extra_obs.get(rid, [])
        acted = min(winfo["times"]).strftime("%Y-%m-%dT%H:%M:%SZ") if winfo.get("times") else None
        fallback = winfo.get("category")
        if link.how == "reference":
            fallback = SYSTEM_CATEGORY.get(link.system)
            rep_t = ts(req["reported_at"])
            acts = sorted(a["event_time"] for a in self.activity_by_resource.get(rid, [])
                          if a["source_system"] == "deletion_worker" and a["event_time"] <= audit_at
                          and abs((ts(a["event_time"]) - rep_t).total_seconds()) <= 3 * 3600)
            acted = acts[0] if acts else None
        return resolve_state(rid, link, obs, self.ev.scopes, lower, audit_at, self.anonymize_categories,
                             self.redact_categories, fallback, acted)

    def _post_cutoff_retention(self, req, uid, states):
        from .model import Step
        have = {s.resource_id for s in states if s.category in self.retain_categories}
        if have:
            return []
        tokens = self.ds.identities.by_user[uid].link_tokens
        out = {}
        for ob in self.ev.observations:
            if (ob.category in self.retain_categories and ob.file_captured_at > req["audit_at"]
                    and ob.observed_at <= req["audit_at"] and ob.observed_at >= req["requested_at"]
                    and ob.exists and ob.link_values & tokens):
                out[ob.resource_id] = Step("VERIFICATION", ob.system, ob.resource_id, "VERIFY_RECORD_PRESENT", "")
        return list(out.values())


def run(dataset_dir: Path, requests_path: Optional[Path] = None, config: Optional[Config] = None,
        engine: str = "agent", max_queries: int = 256):
    """Shared preparation once, then one investigation per request.

    engine="agent" (default) runs the bounded tool-based controller (agent.AuditAgent);
    engine="pipeline" runs the original single-pass Auditor.audit, kept as the reference
    implementation for equivalence tests.
    """
    ds = load_dataset(dataset_dir, requests_path)
    ev = load_evidence(dataset_dir, ds.manifest)
    auditor = Auditor(ds, ev, config)
    universe = {r["case_id"]: r for r in ds.requests}
    for name in ("deletion_requests.csv", "sample_deletion_requests.csv"):
        path = Path(dataset_dir) / name
        if path.is_file():
            for r in read_csv(path):
                universe.setdefault(r["case_id"], r)
    auditor.prepare([universe[k] for k in sorted(universe)])
    if engine == "pipeline":
        return [auditor.audit(r) for r in ds.requests], ds, ev
    from .agent import AuditAgent
    agent = AuditAgent(auditor, max_queries=max_queries)
    return [agent.investigate_case(r) for r in ds.requests], ds, ev
