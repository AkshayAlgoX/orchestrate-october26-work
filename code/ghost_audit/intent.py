"""Deletion-authorization classifier for request_text.

The classifier is deterministic and explainable. It does not fire on a bare
keyword: a request authorizes deletion only when a sentence expresses an
erasure action on the account/personal data, and no sentence scopes the
request away from deletion (explicit negation, "keep the account active",
"only" a narrower action such as marketing opt-out, access, export or
correction). Six languages present in the data are covered (en, es, pt, fr,
ja, ko). Unknown or ambiguous text is NOT treated as authorization.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List

# Erasure actions applied to the account or personal data.
ERASURE = [
    r"\b(delete|deleted|deleting|deletion|erase|erased|erasure|remove|removed|removal|purge|gone)\b",
    r"\bclose\b.*\baccount\b|\baccount\b.*\bclos",
    r"\b(eliminaci[oó]n|eliminar|borrar|supresi[oó]n)\b",           # es
    r"\b(exclus[aã]o|excluir|apagar|elimina[cç][aã]o)\b",           # pt
    r"\b(suppression|supprimer|effacement|effacer)\b",              # fr
    r"削除|抹消",                                                    # ja
    r"삭제|탈퇴",                                                    # ko
]
TARGET = [
    r"\baccount\b", r"\bprofile\b", r"\bpersonal (data|information|details)\b", r"\bdata\b",
    r"\brecords?\b", r"\b(search )?results?\b", r"\bexport( file)?\b", r"\bfiles?\b", r"\bcop(y|ies)\b",
    r"\btraces?\b", r"\banalytics\b", r"\bdocuments?\b", r"\bidentifiers?\b", r"\barchives?\b",
    r"\bcuenta\b", r"\bconta\b", r"\bcompte\b", r"\bperfil\b", r"\bprofil\b", r"アカウント", r"プロフィール",
    r"계정", r"프로필", r"datos", r"dados", r"donn[ée]es",
]
# Statements that scope the request away from deletion.
NEGATIVE_SCOPE = [
    r"\bnot (asking|requesting) (for )?(deletion|to (delete|erase|close))",
    r"\bnot a request to (erase|delete|close)",
    r"\b(do not|don't|dont|never) (close|delete|erase|remove|change)",
    r"\bwithout (removing|deleting|closing)",
    r"\b(keep|leave) (the |my )?(account|profile)\b.*\b(active|as it is|in place|open)",
    r"\bleave my profile\b.*\bin place",
    r"\bcontinue using\b", r"\bstill (need|use|want)\b",
    r"\bmarketing lists only\b", r"\bonly\b.*\b(marketing|newsletter|promotional)",
    r"\bno (quiero|deseo) (eliminar|borrar|cerrar)", r"\bn[aã]o (quero|desejo) (excluir|apagar|encerrar)",
    r"\bne (pas|veux pas) (supprimer|fermer)", r"削除しないで|削除は不要", r"삭제하지",
]
# Narrower request types that are not deletion on their own.
OTHER_ACTION = [
    r"\b(stop|unsubscribe).*\b(email|emails|marketing|promotional)",
    r"\b(download|export|portable|copy of)\b", r"\bwhat (personal )?information you hold\b",
    r"\b(correct|update|replace|change)\b.*\b(email|surname|name|number|phone|address)",
    r"\b(reset|access)\b.*\b(login|password|profile)", r"\binvoices?\b",
]


@dataclass
class IntentResult:
    authorizes_deletion: bool
    reason: str


def _sentences(text: str) -> List[str]:
    parts = re.split(r"(?<=[.!?。])\s*|\n+", text.strip())
    return [p.strip() for p in parts if p and p.strip()]


def _any(patterns, s: str) -> bool:
    return any(re.search(p, s, re.I) for p in patterns)


def classify(text: str) -> IntentResult:
    sents = _sentences(text or "")
    if not sents:
        return IntentResult(False, "empty request text")
    negative = [s for s in sents if _any(NEGATIVE_SCOPE, s)]
    erasure = [s for s in sents if _any(ERASURE, s) and _any(TARGET, s) and not _any(NEGATIVE_SCOPE, s)]
    other = [s for s in sents if _any(OTHER_ACTION, s)]
    if negative:
        return IntentResult(False, f"explicit non-deletion scope: {negative[0]!r}")
    if erasure:
        return IntentResult(True, f"erasure of account/personal data requested: {erasure[0]!r}")
    if other:
        return IntentResult(False, f"non-deletion action requested: {other[0]!r}")
    return IntentResult(False, "no erasure action on the account or personal data was expressed")
