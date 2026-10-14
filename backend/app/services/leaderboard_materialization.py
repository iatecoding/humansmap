"""Materialize expensive organization leaderboard aggregates in ArangoDB."""

from __future__ import annotations

from datetime import datetime, timezone
import logging

from app.database import AFFILIATIONS, APP_METADATA, ORGANIZATIONS
from app.services.cache import flush_pattern

logger = logging.getLogger(__name__)

MARKER_KEY = "leaderboard_org_counts"
MATERIALIZATION_VERSION = 1
EXCLUDED_AFFILIATION_TYPES = ["insider_of", "holds_shares_of"]


def _write_marker(db, status: str, **extra) -> None:
    metadata = db.collection(APP_METADATA)
    doc = {
        "_key": MARKER_KEY,
        "status": status,
        "version": MATERIALIZATION_VERSION,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        **extra,
    }
    if metadata.has(MARKER_KEY):
        metadata.update(doc)
    else:
        metadata.insert(doc)


def refresh_org_affiliation_counts(db, *, invalidate_cache: bool = True) -> int:
    """Rebuild organization affiliation counts with a safe readiness marker.

    Requests keep using the correct aggregation fallback while ``status`` is
    ``building``. Only the final marker switches the API to indexed document
    reads, so an interrupted refresh can never expose a partially rebuilt rank.
    """
    if not db.has_collection(APP_METADATA):
        db.create_collection(APP_METADATA)

    _write_marker(db, "building")
    try:
        db.aql.execute(f"""
            FOR org IN {ORGANIZATIONS}
                FILTER org.affiliation_count != null
                UPDATE org WITH {{ affiliation_count: null }} IN {ORGANIZATIONS}
        """)
        db.aql.execute(
            f"""
                FOR edge IN {AFFILIATIONS}
                    FILTER STARTS_WITH(edge._to, "{ORGANIZATIONS}/")
                    FILTER edge.type NOT IN @excluded
                    COLLECT org_id = edge._to WITH COUNT INTO affiliation_count
                    LET org = DOCUMENT(org_id)
                    FILTER org != null
                    UPDATE org WITH {{ affiliation_count: affiliation_count }}
                        IN {ORGANIZATIONS}
            """,
            bind_vars={"excluded": EXCLUDED_AFFILIATION_TYPES},
        )
        count = next(iter(db.aql.execute(f"""
            RETURN LENGTH(
                FOR org IN {ORGANIZATIONS}
                    FILTER org.affiliation_count > 0
                    RETURN 1
            )
        """)), 0)
        _write_marker(db, "ready", organizations_ranked=count)
    except Exception as exc:
        logger.exception("Organization leaderboard materialization failed")
        try:
            _write_marker(db, "failed", error=str(exc)[:500])
        except Exception:
            logger.exception("Could not record leaderboard materialization failure")
        raise

    if invalidate_cache:
        flush_pattern("leaderboard:*")
    logger.info("Materialized affiliation counts for %d organizations", count)
    return count
