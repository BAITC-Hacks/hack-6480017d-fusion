"""Bounded AI response schemas and reference validation, not truth verification."""
from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, str_strip_whitespace=True)


ShortText = Annotated[str, StringConstraints(min_length=1, max_length=500)]
EvidenceId = Annotated[str, StringConstraints(min_length=1, max_length=80)]
EvidenceIds = Annotated[list[EvidenceId], Field(min_length=1, max_length=8)]
ClaimId = Annotated[str, StringConstraints(pattern=r'^c[1-6]$')]


class AnalystClaim(StrictModel):
    claim_id: ClaimId
    text: ShortText
    evidence_ids: EvidenceIds


class AnalystReport(StrictModel):
    hypothesis: Annotated[str, StringConstraints(min_length=1, max_length=800)]
    claims: Annotated[list[AnalystClaim], Field(min_length=1, max_length=6)]
    limitations: Annotated[list[ShortText], Field(min_length=1, max_length=8)]
    next_checks: Annotated[list[ShortText], Field(min_length=1, max_length=6)]


class ReviewerIssue(StrictModel):
    claim_id: Annotated[str, StringConstraints(pattern=r'^(c[1-6]|overall)$')]
    kind: Literal['unsupported_claim', 'missing_caveat', 'overstatement', 'contradiction']
    reason: Annotated[str, StringConstraints(min_length=1, max_length=600)]
    evidence_ids: EvidenceIds


class ReviewerReport(StrictModel):
    issues: Annotated[list[ReviewerIssue], Field(max_length=8)]
    alternative_explanations: Annotated[list[ShortText], Field(max_length=5)]
    missing_information: Annotated[list[ShortText], Field(max_length=6)]


def _known_references(packet: dict) -> set[str]:
    return {
        item['evidence_id']
        for section in ('facts', 'neighbors', 'caveats')
        for item in packet[section]
    }


def _validate_refs(references: list[str], allowed: set[str]) -> None:
    if not set(references).issubset(allowed):
        raise ValueError('Ответ содержит ссылку на неизвестный факт')


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _validate_gids(payload: dict, packet: dict) -> None:
    allowed = {packet['gid']}
    for edge in packet['neighbors']:
        allowed.update((edge['src'], edge['dst']))
    # Ignore decimal fractions: PageRank may have a long fractional part.
    # This guard detects invented long integer IDs, not arbitrary false prose.
    for text in _strings(payload):
        for match in re.finditer(r'(?<!\d)\d{16,}(?!\d)', text):
            offset = match.start()
            fractional = offset >= 2 and text[offset - 1] in '.,' and text[offset - 2].isdigit()
            if not fractional and match.group() not in allowed:
                raise ValueError('Ответ содержит идентификатор вне пакета фактов')


def validate_analyst(payload: dict, packet: dict) -> dict:
    report = AnalystReport.model_validate(payload).model_dump()
    claim_ids = [claim['claim_id'] for claim in report['claims']]
    if len(claim_ids) != len(set(claim_ids)):
        raise ValueError('Идентификаторы утверждений должны быть уникальными')
    allowed = _known_references(packet)
    for claim in report['claims']:
        _validate_refs(claim['evidence_ids'], allowed)
    _validate_gids(report, packet)
    return report


def validate_reviewer(payload: dict, packet: dict, report: dict) -> dict:
    review = ReviewerReport.model_validate(payload).model_dump()
    allowed_claims = {claim['claim_id'] for claim in report['claims']} | {'overall'}
    allowed_refs = _known_references(packet)
    for issue in review['issues']:
        if issue['claim_id'] not in allowed_claims:
            raise ValueError('Рецензия ссылается на отсутствующее утверждение')
        _validate_refs(issue['evidence_ids'], allowed_refs)
    _validate_gids(review, packet)
    return review
