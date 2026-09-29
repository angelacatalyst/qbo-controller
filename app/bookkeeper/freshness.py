"""Say whether a figure is live QuickBooks or a stored snapshot."""
from datetime import datetime, timedelta

STALE_AFTER = timedelta(hours=24)


def describe_snapshot(company, profile, now: datetime | None = None) -> dict:
    """Label cached reports. A snapshot is never reported as live."""
    moment = now or datetime.utcnow()
    synced = getattr(company, "last_sync", None)
    as_of = getattr(profile, "data_as_of", None) if profile else None
    anchor = as_of or synced
    if anchor is None:
        return {
            "source": "NONE",
            "freshness": "MISSING",
            "last_sync": None,
            "data_as_of": None,
            "stale": True,
            "label": "No QBO snapshot. Sync this company before relying on reports.",
        }
    age = moment - anchor
    stale = age > STALE_AFTER
    freshness = "STALE" if stale else "CACHED"
    return {
        "source": "CACHED_SYNC",
        "freshness": freshness,
        "last_sync": synced.isoformat() if synced else None,
        "data_as_of": as_of.isoformat() if as_of else None,
        "stale": stale,
        "label": (
            "Cached QBO snapshot"
            + (" older than 24 hours." if stale else ".")
            + " Report figures are not a live read."
        ),
    }


def live_read(entity: str) -> dict:
    return {
        "source": "LIVE_QBO",
        "freshness": "LIVE",
        "entity": entity,
        "label": f"Live QBO read of {entity} during this run.",
    }
