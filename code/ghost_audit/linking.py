"""Subject <-> resource attribution with provenance.

A resource is attributed to a case subject only through grounded links:
  1. direct  - an eligible observation carries one of the subject's identifier
               tokens (current/historical email, email hash, phone, user_id),
               including tokens joined through the same log trace;
  2. catalog - a LINK_RESOURCE_IDENTITY activity names the subject, and an
               eligible observation/document of that resource exists whose own
               identifiers do not contradict the claim;
  3. workflow (optional, see `workflow_links`) - identifier-less resources
               (404 lookups, anonymized rows) that the deletion workflow acted
               on for this request and that appear in the same evidence file as
               the subject's directly linked records.
Names alone never link. Lookalike identifiers never link (exact tokens only).
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple

from .loaders import Identity, IdentityIndex, norm_token
from .model import Observation

CATALOG_RE = re.compile(
    r"(?:Data|Attachment) catalog (?:linked|confirmed) (?P<rid>[a-z_]+_\d+) \((?P<ref>[^)]*)\) "
    r"(?:to|belongs only to) (?P<email>[^\s]+@[^\s]+) and (?P<user>user_\d+)")


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


@dataclass
class Link:
    resource_id: str
    system: str
    how: str                  # direct | catalog | workflow
    basis: List[str] = field(default_factory=list)   # file_ids / activity_ids supporting the link


@dataclass
class CatalogClaim:
    activity_id: str
    event_time: str
    target_system: str
    resource_id: str
    ref: str
    email: str
    user: str
    exclusive: bool


def catalog_claims(activity: List[Dict[str, str]]) -> List[CatalogClaim]:
    out = []
    for a in activity:
        if a["event_type"] != "LINK_RESOURCE_IDENTITY":
            continue
        m = CATALOG_RE.search(a["details"])
        if not m:
            continue
        out.append(CatalogClaim(a["activity_id"], a["event_time"], a["target_system"], m["rid"], m["ref"],
                                m["email"].rstrip(".;"), m["user"], "belongs only to" in a["details"]))
    return out


def eligible(ob: Observation, lower: Optional[str], audit_at: str) -> bool:
    """Observation usable for the audited state of a case.

    The file must be captured at or before audit_at (inclusive) and the record
    time must not be after audit_at. `lower` optionally excludes evidence
    captured before the request window.
    """
    if ob.file_captured_at > audit_at or ob.sort_time > audit_at:
        return False
    if lower and ob.file_captured_at < lower:
        return False
    return True


def direct_links(ident: Identity, observations: List[Observation], lower: Optional[str],
                 audit_at: str, index: IdentityIndex) -> Dict[str, Link]:
    tokens = ident.link_tokens
    links: Dict[str, Link] = {}
    for ob in observations:
        if not eligible(ob, lower, audit_at):
            continue
        hit = ob.link_values & tokens
        if not hit:
            continue
        link = links.setdefault(ob.resource_id, Link(ob.resource_id, ob.system, "direct"))
        if ob.file_id not in link.basis:
            link.basis.append(ob.file_id)
    return links


def catalog_links(ident: Identity, claims: List[CatalogClaim], by_resource: Dict[str, List[Observation]],
                  documents, lower: Optional[str], audit_at: str, index: IdentityIndex) -> Dict[str, dict]:
    """Return resource_id -> {'link': Link, 'observations': [synthetic obs from documents]}."""
    out: Dict[str, dict] = {}
    for c in claims:
        if c.event_time > audit_at:
            continue
        if norm_token(c.email) not in ident.emails and c.user != ident.user_id:
            continue
        # The claim's own identifiers must not contradict each other: an email that resolves
        # to a known person other than the named user (or a named user who is a different
        # known person than the email's owner) is a contradictory claim and is rejected.
        # A partial claim (one identifier unknown to the identity tables) is still usable.
        email_owner = index.resolve_email(c.email)
        user_known = c.user in index.by_user
        if email_owner is not None and user_known and email_owner != c.user:
            continue
        support = [o for o in by_resource.get(c.resource_id, []) if eligible(o, lower, audit_at)]
        docs = [d for d in documents.get(c.ref, [])
                if d["captured_at"] <= audit_at and (not lower or d["captured_at"] >= lower)]
        if not support and not docs:
            continue
        # Contradiction check: displayed identifiers that belong to nobody/somebody else.
        contradicted = False
        for o in support:
            shown = {norm_token(v) for k, v in o.pii.items() if k in ("email", "phone", "email_hash")}
            shown.discard("")
            if shown and not (shown & ident.link_tokens):
                contradicted = True
        for d in docs:
            shown = {norm_token(v) for k, v in d["fields"].items() if k in ("email", "phone", "subject_reference")}
            shown.discard("")
            if shown and not (shown & ident.link_tokens):
                contradicted = True
        if contradicted:
            continue
        basis = sorted({o.file_id for o in support} | {d["file_id"] for d in docs}) + [c.activity_id]
        out[c.resource_id] = {"link": Link(c.resource_id, c.target_system, "catalog", basis),
                              "claim": c, "docs": docs}
    return out


def workflow_candidates(case: Dict[str, str], direct: Dict[str, Link], by_resource: Dict[str, List[Observation]],
                        activity_by_resource, lower: Optional[str], audit_at: str, owned: Set[str],
                        before_h: float = 2.0, after_h: float = 2.5,
                        activity_only: Optional[Set[str]] = None,
                        relaxed_colocation: bool = False) -> Dict[str, dict]:
    """Identifier-less resources the deletion workflow acted on around this request.

    Candidate requirements (all must hold):
      * a deletion_worker activity on the resource falls within
        [reported_at - before_h, reported_at + after_h];
      * an eligible observation of the resource carries no identifying tokens
        at all (404 lookup, anonymized row), so no content link is possible;
      * that observation is in an evidence file that also holds a directly
        linked record of this subject (same capture);
      * the resource is not attributed by content to any person.
    With `activity_only` (a set of categories), resources that no evidence file
    mentions at all are also candidates; their state is then unknown and is
    handled conservatively by the planner.
    Candidates are claims about workflow scope, not proof; `resolve_workflow`
    assigns them only when the assignment is unambiguous.
    """
    rep = ts(case["reported_at"])
    lo, hi = rep - timedelta(hours=before_h), rep + timedelta(hours=after_h)
    subject_files = {f for link in direct.values() for f in link.basis}
    out: Dict[str, dict] = {}
    for rid, acts in activity_by_resource.items():
        if rid in direct or rid in owned:
            continue
        hits = [a for a in acts if a["source_system"] == "deletion_worker" and lo <= ts(a["event_time"]) <= hi]
        if not hits:
            continue
        obs = [o for o in by_resource.get(rid, []) if eligible(o, lower, audit_at)]
        anon = [o for o in obs if not o.link_values
                and (o.file_id in subject_files or (relaxed_colocation and not subject_files))]
        if anon:
            cats = [o.category for o in anon if o.category]
            out[rid] = {"link": Link(rid, hits[0]["target_system"], "workflow",
                                     sorted({o.file_id for o in anon}) + [a["activity_id"] for a in hits]),
                        "times": [ts(a["event_time"]) for a in hits],
                        "category": max(sorted(set(cats)), key=cats.count) if cats else None, "observed": True}
        elif activity_only and not any(o.file_captured_at <= audit_at for o in by_resource.get(rid, [])):
            # Known only from the workflow stream (no evidence file mentions it).
            from .evidence import SYSTEM_CATEGORY
            cat = SYSTEM_CATEGORY.get(hits[0]["target_system"])
            if cat not in activity_only:
                continue
            out[rid] = {"link": Link(rid, hits[0]["target_system"], "workflow",
                                     [a["activity_id"] for a in hits]),
                        "times": [ts(a["event_time"]) for a in hits], "category": cat, "observed": False}
    return out


# Categories where a subject holds at most one record (one profile per user). Supported by the
# corpus: no subject has two in-episode USER_PROFILE or ANALYTICS_PROFILE records. Other
# categories (support, search, exports, ...) can legitimately be multiple per subject.
CAPACITY_ONE = frozenset({"USER_PROFILE", "ANALYTICS_PROFILE"})


def one_per_request(inf: dict) -> bool:
    """Whether a workflow candidate counts against a one-per-request limit in its category.

    Profile categories and unknown categories always do. So does a candidate known only from
    deletion-workflow activity (no evidence file mentions it): nothing co-locates it with the
    subject's own records, and the limit is then the only guard against taking another person's
    record. Observed, co-located candidates of other categories may be several per request.
    """
    return inf["category"] is None or inf["category"] in CAPACITY_ONE or not inf.get("observed", True)


def resolve_workflow(candidates: Dict[str, Dict[str, dict]], direct_categories: Dict[str, Set[str]],
                     anchor_times: Dict[str, List[datetime]], proximity_margin_min: Optional[float] = None,
                     ambiguous: str = "drop") -> Dict[str, Dict[str, Link]]:
    """Assign identifier-less resources to cases.

    candidates: case_id -> rid -> info. A resource is assigned only when a single case
    remains possible; optionally, a remaining tie is broken by the nearest worker
    activity on the case's directly linked resources when the nearest case is closer
    than every other by `proximity_margin_min` minutes. Two passes:
      1. resources that no candidate limits to one per request (`one_per_request`) do
         not constrain one another and are decided individually; their categories
         then count as held;
      2. limited resources are resolved by constraint propagation: a case may hold at
         most one resource per category (direct links and pass-1 assignments included).
    Everything else stays unassigned (reported as unresolved).
    """
    by_rid: Dict[str, Set[str]] = defaultdict(set)
    info: Dict[Tuple[str, str], dict] = {}
    for cid, rids in candidates.items():
        for rid, inf in rids.items():
            by_rid[rid].add(cid)
            info[(cid, rid)] = inf
    used: Dict[str, Set[str]] = {cid: set(cats) for cid, cats in direct_categories.items()}
    assigned: Dict[str, Dict[str, Link]] = defaultdict(dict)

    def cat(cid, rid):
        return info[(cid, rid)]["category"]

    def limited(cid, rid):
        return one_per_request(info[(cid, rid)])

    def prune():
        # Only pass-2 resources remain in by_rid, and each has a limited candidate, so the
        # resource as a whole is one per request (as before this change).
        for rid in list(by_rid):
            by_rid[rid] = {c for c in by_rid[rid] if cat(c, rid) not in used.get(c, set())}

    def nearest(rid):
        """The candidate case whose worker anchors are nearest, if it beats every other by the margin."""
        if proximity_margin_min is None or not all(info[(c, rid)].get("observed", True) for c in by_rid[rid]):
            return None  # never tie-break resources known only from activity
        dists = []
        for c in by_rid[rid]:
            anchors = anchor_times.get(c, [])
            if anchors:
                dists.append((min(abs((a - t).total_seconds()) / 60 for a in anchors for t in info[(c, rid)]["times"]),
                              c))
        dists.sort()
        if len(dists) == len(by_rid[rid]) and len(dists) >= 2 and dists[1][0] - dists[0][0] >= proximity_margin_min:
            return dists[0][1]
        return None

    # Pass 1: resources that no candidate limits to one per request (observed, co-located records of
    # categories a subject can hold several of) do not constrain one another, so each is decided on
    # its own and the order is irrelevant. Their categories then count as held, like direct links,
    # for the limited candidates resolved below.
    open_unlimited: Dict[str, Set[str]] = {}
    for rid in sorted(by_rid):
        if any(limited(c, rid) for c in by_rid[rid]):
            continue
        owner = next(iter(by_rid[rid])) if len(by_rid[rid]) == 1 else nearest(rid)
        if owner is None:
            open_unlimited[rid] = by_rid[rid]
        else:
            assigned[owner][rid] = info[(owner, rid)]["link"]
        del by_rid[rid]
    for c, rids in assigned.items():
        used.setdefault(c, set()).update(cat(c, rid) for rid in rids)

    def propagate():
        changed = True
        while changed:
            changed = False
            prune()
            for rid in sorted(by_rid):
                cs = by_rid[rid]
                if len(cs) == 1:
                    c = next(iter(cs))
                    rivals = [r for r in by_rid if r != rid and by_rid[r] == {c} and cat(c, r) == cat(c, rid)]
                    if rivals:
                        # Several resources of one limited category can only belong to c:
                        # choosing one by identifier order would be arbitrary, so none is assigned.
                        for r in [rid] + rivals:
                            del by_rid[r]
                        changed = True
                        break
                    assigned[c][rid] = info[(c, rid)]["link"]
                    used.setdefault(c, set()).add(cat(c, rid))
                    del by_rid[rid]
                    changed = True
                    break
                if not cs:
                    del by_rid[rid]
                    changed = True
                    break

    propagate()
    if proximity_margin_min is not None:
        progress = True
        while progress and by_rid:
            progress = False
            for rid in sorted(by_rid):
                owner = nearest(rid)
                if owner is not None:
                    by_rid[rid] = {owner}
                    propagate()
                    progress = True
                    break
    if ambiguous == "all":
        for rid, cs in list(by_rid.items()) + list(open_unlimited.items()):
            for c in cs:
                assigned[c][rid] = info[(c, rid)]["link"]
    return assigned
