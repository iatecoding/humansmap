"""Build `holds_shares_of` Org→Org edges from aggregated 13F holdings.

§11.4.c — pure edge construction + optional DB upsert. Quarterly snapshots
share the same `_key = {manager}__{issuer}__{period_of_report}` so re-loading
the same filing is idempotent; a later-amended 13F/A overwrites the prior row.

Inputs come from parse_13f + aggregate_by_cusip (11.4.a) and the CusipResolver
(11.4.b). Unmatched CUSIPs are collected into `sec_unmatched_holdings` on the
manager org (capped), mirroring the DEF 14A unmatched-names denorm pattern.
"""

from __future__ import annotations

import logging
from typing import Iterable

from .parse_13f import Holding

logger = logging.getLogger("write_13f_edges")

UNMATCHED_CAP = 500  # more generous than directors — 13F rosters are longer


def build_edge_docs(
    *,
    manager_qid: str,
    period_of_report: str,
    filing_date: str,
    accession: str,
    holdings_by_cusip: dict[str, Holding],
    resolver,
    is_amendment: bool = False,
) -> tuple[list[dict], list[dict]]:
    """Return (edges, unmatched) for a single 13F filing.

    - ``holdings_by_cusip`` is the output of ``aggregate_by_cusip`` — one row
      per issuer CUSIP, sub-adviser splits already summed.
    - ``resolver`` is any object with ``.resolve(cusip, name_of_issuer) -> qid | None``.
    - Rows whose resolver returns None go to ``unmatched`` (list of dicts with
      cusip + name_of_issuer + value_usd + shares). Callers merge into
      ``sec_unmatched_holdings`` on the manager org.
    - Self-holdings (manager == issuer) are dropped; they're common in 13F
      (fund-of-fund cross-holdings) and carry no new graph info.

    Edge key is stable per (manager, issuer, period), so an amended 13F/A
    loaded later overwrites the prior quarterly row. ``is_amendment`` is
    recorded on the edge for downstream filtering but doesn't change key shape.
    """
    edges: list[dict] = []
    unmatched: list[dict] = []

    for cusip, h in holdings_by_cusip.items():
        issuer_qid = resolver.resolve(cusip, h.name_of_issuer)
        if not issuer_qid:
            unmatched.append({
                "cusip": cusip,
                "name_of_issuer": h.name_of_issuer,
                "value_usd": h.value_usd,
                "shares": h.shares,
                "title_of_class": h.title_of_class,
            })
            continue

        if issuer_qid == manager_qid:
            continue  # skip self-holdings

        edges.append({
            "_key": f"{manager_qid}__{issuer_qid}__{period_of_report}",
            "_from": f"organizations/{manager_qid}",
            "_to": f"organizations/{issuer_qid}",
            "type": "holds_shares_of",
            "cusip": cusip,
            "shares": h.shares,
            "value_usd": h.value_usd,
            "shares_type": h.shares_type,
            "title_of_class": h.title_of_class,
            "period_of_report": period_of_report,
            "filing_date": filing_date,
            "accession": accession,
            "form_type": "13F-HR/A" if is_amendment else "13F-HR",
            "is_amendment": is_amendment,
            "source": "sec_13f",
        })

    return edges, unmatched


def upsert_edges(db, affiliations_collection: str, edges: list[dict]) -> None:
    """Idempotent bulk upsert. Uses overwriteMode=update so amended filings win."""
    if not edges:
        return
    aql = f"""
    FOR doc IN @batch
      INSERT doc INTO {affiliations_collection}
      OPTIONS {{ overwriteMode: "update" }}
    """
    db.aql.execute(aql, bind_vars={"batch": edges})


def merge_unmatched_holdings(
    db,
    organizations_collection: str,
    manager_qid: str,
    new_unmatched: Iterable[dict],
    cap: int = UNMATCHED_CAP,
) -> int:
    """Merge new unmatched CUSIP rows into the manager org's denorm list.

    Deduplicates by CUSIP. Newer entries win on conflict (last-write-wins).
    Returns the final length of the merged list.
    """
    new_list = list(new_unmatched)
    if not new_list:
        return 0

    org_doc = db.collection(organizations_collection).get(manager_qid) or {}
    existing: list[dict] = org_doc.get("sec_unmatched_holdings") or []

    by_cusip: dict[str, dict] = {r.get("cusip"): r for r in existing if r.get("cusip")}
    for r in new_list:
        c = r.get("cusip")
        if c:
            by_cusip[c] = r

    merged = list(by_cusip.values())
    if len(merged) > cap:
        # Keep highest-value entries — the interesting tail
        merged.sort(key=lambda r: r.get("value_usd") or 0, reverse=True)
        merged = merged[:cap]

    db.collection(organizations_collection).update(
        {"_key": manager_qid, "sec_unmatched_holdings": merged}
    )
    return len(merged)
