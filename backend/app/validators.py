"""Shared request validators."""

import re

from fastapi import HTTPException, Path


def valid_qid(qid: str = Path(...)) -> str:
    """Validate a Wikidata QID or an occupation slug.

    FastAPI uses 422 for malformed path/query input, so all endpoints sharing
    this dependency expose the same validation contract.
    """
    if not re.fullmatch(r"Q[0-9]+|[a-z][a-z0-9_]*", qid):
        raise HTTPException(422, f"Invalid identifier format: {qid}")
    return qid


def valid_event_id(qid: str = Path(...)) -> str:
    """Validate Wikidata event IDs and SEC accession-derived event keys."""
    if not re.fullmatch(r"Q[0-9]+|[0-9]{6,}", qid):
        raise HTTPException(422, f"Invalid event identifier format: {qid}")
    return qid
