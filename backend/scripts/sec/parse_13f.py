"""Parse SEC 13F-HR `informationTable` XML into flat holding rows.

§11.4.a — pure parser, no I/O, no DB. Uses lxml (added in 11.1.a).

Schema (stable since 2013, XSD-enforced by SEC):
    <informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
      <infoTable>
        <nameOfIssuer>ALLY FINL INC</nameOfIssuer>
        <titleOfClass>COM</titleOfClass>
        <cusip>02005N100</cusip>
        <value>576074081</value>            <!-- USD, thousands pre-2023, full dollars post -->
        <shrsOrPrnAmt>
          <sshPrnamt>12719675</sshPrnamt>
          <sshPrnamtType>SH</sshPrnamtType>   <!-- SH or PRN -->
        </shrsOrPrnAmt>
        ...
      </infoTable>
    </informationTable>

Namespace prefix varies ("ns1:", default, none). We strip the namespace before
matching local names — cheapest approach.

Multiple infoTable rows can reference the same CUSIP (sub-adviser splits).
Callers deduplicate by (cusip, period_of_report, manager_cik).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

try:
    from lxml import etree
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "lxml is required for 13F parsing. Rebuild the backend image — "
        "lxml was added in 11.1.a."
    ) from exc


# Post-2023 SEC rule: `value` is in full dollars. Pre-2023: thousands.
# The filer indicates this via the cover-page XML (primary doc, not the
# information table). We default to full dollars (post-2023 filings are
# the common case for fresh ingestion) and let the bulk loader override
# via a flag sourced from the cover page if/when needed.
DEFAULT_VALUE_IS_THOUSANDS = False


@dataclass(frozen=True)
class Holding:
    """One row of the information table — the thing that becomes an edge."""
    name_of_issuer: str
    cusip: str
    value_usd: int          # USD (normalized; see DEFAULT_VALUE_IS_THOUSANDS)
    shares: int             # sshPrnamt
    shares_type: str        # "SH" (equity) or "PRN" (principal amount / bond)
    title_of_class: str


def _local(tag: str) -> str:
    """Strip XML namespace: ``{ns}localName`` → ``localName``."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _text(el, child_local: str) -> str:
    """Return text of first child whose local-name matches, or ''. ``el`` may be None."""
    if el is None:
        return ""
    for c in el:
        if _local(c.tag) == child_local:
            return (c.text or "").strip()
    return ""


def _nested_text(el, outer: str, inner: str) -> str:
    if el is None:
        return ""
    for c in el:
        if _local(c.tag) == outer:
            return _text(c, inner)
    return ""


def _parse_int(value: str) -> int:
    """Parse a stringy int, tolerating commas and whitespace. Return 0 on empty."""
    s = (value or "").replace(",", "").strip()
    if not s:
        return 0
    return int(float(s))  # float() tolerates "1.0" style numerics


def parse_13f_xml(
    source: bytes | str,
    *,
    value_is_thousands: bool = DEFAULT_VALUE_IS_THOUSANDS,
) -> list[Holding]:
    """Parse a 13F information-table XML document into a list of Holdings.

    ``source`` may be raw bytes, a filesystem path, or a decoded string.
    Namespace-agnostic (strips ns prefix before matching local names).

    >>> xml = b'''<?xml version="1.0"?>
    ... <informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
    ...   <infoTable>
    ...     <nameOfIssuer>APPLE INC</nameOfIssuer>
    ...     <titleOfClass>COM</titleOfClass>
    ...     <cusip>037833100</cusip>
    ...     <value>100000</value>
    ...     <shrsOrPrnAmt><sshPrnamt>500</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
    ...   </infoTable>
    ... </informationTable>'''
    >>> out = parse_13f_xml(xml)
    >>> len(out), out[0].cusip, out[0].value_usd, out[0].shares
    (1, '037833100', 100000, 500)
    """
    if isinstance(source, str) and source.strip().startswith("<"):
        source_bytes: bytes = source.encode("utf-8")
    elif isinstance(source, bytes):
        source_bytes = source
    else:
        # Filesystem path
        with open(source, "rb") as fh:
            source_bytes = fh.read()

    # recover=True lets us tolerate stray whitespace / PI nodes some filers add
    parser = etree.XMLParser(recover=True, huge_tree=True)
    root = etree.fromstring(source_bytes, parser)
    if root is None:
        return []

    holdings: list[Holding] = []
    for el in root:
        if _local(el.tag) != "infoTable":
            continue

        name = _text(el, "nameOfIssuer")
        title = _text(el, "titleOfClass")
        cusip = _text(el, "cusip").upper()
        value_raw = _parse_int(_text(el, "value"))
        shares = _parse_int(_nested_text(el, "shrsOrPrnAmt", "sshPrnamt"))
        shares_type = _nested_text(el, "shrsOrPrnAmt", "sshPrnamtType") or "SH"

        if not cusip or not name:
            continue  # malformed row; skip silently

        value_usd = value_raw * 1_000 if value_is_thousands else value_raw

        holdings.append(Holding(
            name_of_issuer=name,
            cusip=cusip,
            value_usd=value_usd,
            shares=shares,
            shares_type=shares_type,
            title_of_class=title,
        ))

    return holdings


def aggregate_by_cusip(holdings: list[Holding]) -> dict[str, Holding]:
    """Collapse sub-adviser splits: sum shares + value per CUSIP.

    The 13F information table can contain multiple rows for the same CUSIP
    (e.g. BlackRock: 48k rows, 5k unique CUSIPs — ~9× multiplier from sub-
    adviser splits). For Org→Org edges we want one row per issuer.

    Assumes all rows for a given CUSIP share the same name/title/shares_type;
    we take those from the first-seen row and sum the numeric fields.
    """
    out: dict[str, Holding] = {}
    for h in holdings:
        existing = out.get(h.cusip)
        if existing is None:
            out[h.cusip] = h
        else:
            out[h.cusip] = Holding(
                name_of_issuer=existing.name_of_issuer,
                cusip=existing.cusip,
                value_usd=existing.value_usd + h.value_usd,
                shares=existing.shares + h.shares,
                shares_type=existing.shares_type,
                title_of_class=existing.title_of_class,
            )
    return out


def iter_top_by_value(holdings: list[Holding], n: int) -> Iterator[Holding]:
    """Yield the top-N holdings by USD value, descending. Convenience for smoke prints."""
    for h in sorted(holdings, key=lambda x: x.value_usd, reverse=True)[:n]:
        yield h
