"""The only door that may change QuickBooks.

Status path for an approved action:
    approved → executing → verified
Failure after a post that cannot be confirmed:
    failed

A proposal sits in pending until a person approves it. Engines never receive
a permit, so they cannot post.
"""
from dataclasses import dataclass

WRITE_PATHS = frozenset({
    "/journalentry",
    "/purchase",
    "/deposit",
    "/salesreceipt",
    "/payment",
    "/bill",
    "/billpayment",
    "/transfer",
    "/vendorcredit",
    "/refundreceipt",
})

_ACCOUNT_DETAIL_FIELDS = (
    "AccountBasedExpenseLineDetail",
    "DepositLineDetail",
    "JournalEntryLineDetail",
    "AccountRef",
)


class WriteRefused(PermissionError):
    """A QBO write was attempted outside the approval path."""


@dataclass(frozen=True)
class WritePermit:
    realm_id: str
    action_id: str
    endpoint: str


def normalize_endpoint(endpoint: str) -> str:
    path = (endpoint or "").split("?")[0].strip().lower()
    if path and not path.startswith("/"):
        path = "/" + path
    return path


def issue_permit(
    *,
    realm_id: str,
    record_realm_id: str,
    status: str,
    action_id: str,
    endpoint: str,
) -> WritePermit:
    """Issue a one-endpoint permit. Raises WriteRefused when the gate fails."""
    if not realm_id or realm_id != record_realm_id:
        raise WriteRefused("Write refused: the record belongs to a different company.")
    if (status or "") != "approved":
        raise WriteRefused(
            f"Write refused: status is '{status or 'missing'}'. Approval is required."
        )
    path = normalize_endpoint(endpoint)
    if path not in WRITE_PATHS:
        raise WriteRefused(f"Write refused: '{path or endpoint}' is not an allowed write.")
    if not action_id:
        raise WriteRefused("Write refused: the action has no id.")
    return WritePermit(realm_id=realm_id, action_id=str(action_id), endpoint=path)


def unwrap_entity(payload: dict, txn_type: str) -> dict:
    """Return the entity object from a QBO GET envelope.

    GET /purchase/{id} returns {"Purchase": {...}, "time": "..."}.
    Posting that envelope back is not an update.
    """
    if not isinstance(payload, dict):
        return {}
    wanted = (txn_type or "").lower()
    for key, value in payload.items():
        if key.lower() == wanted and isinstance(value, dict):
            return value
    if "Id" in payload or "Line" in payload or "SyncToken" in payload:
        return payload
    return {}


def patch_first_account(entity: dict, account_id: str, account_name: str) -> bool:
    """Set AccountRef on the first expense/deposit/journal line. Returns whether a line changed."""
    if not account_id or not isinstance(entity, dict):
        return False
    lines = entity.get("Line") or []
    if isinstance(lines, dict):
        lines = [lines]
    ref = {"value": str(account_id), "name": account_name or ""}
    for line in lines:
        if not isinstance(line, dict):
            continue
        for field in ("AccountBasedExpenseLineDetail", "DepositLineDetail", "JournalEntryLineDetail"):
            detail = line.get(field)
            if isinstance(detail, dict) and "AccountRef" in detail:
                detail["AccountRef"] = ref
                return True
        if isinstance(line.get("AccountRef"), dict):
            line["AccountRef"] = ref
            return True
    return False


def account_is_set(entity: dict, account_id: str) -> bool:
    """True when a re-read shows the proposed account on a line."""
    if not account_id or not isinstance(entity, dict):
        return False
    lines = entity.get("Line") or []
    if isinstance(lines, dict):
        lines = [lines]
    wanted = str(account_id)
    for line in lines:
        if not isinstance(line, dict):
            continue
        for field in ("AccountBasedExpenseLineDetail", "DepositLineDetail", "JournalEntryLineDetail"):
            detail = line.get(field)
            if isinstance(detail, dict):
                value = (detail.get("AccountRef") or {}).get("value")
                if str(value or "") == wanted:
                    return True
        value = (line.get("AccountRef") or {}).get("value") if isinstance(line.get("AccountRef"), dict) else None
        if str(value or "") == wanted:
            return True
    return False


def sparse_body(entity: dict) -> dict:
    """Body safe to POST. Carries Id, SyncToken, and the patched lines."""
    if not entity.get("Id") or entity.get("SyncToken") is None:
        raise WriteRefused("Write refused: QBO object has no Id or SyncToken.")
    return {
        "Id": entity["Id"],
        "SyncToken": entity["SyncToken"],
        "sparse": True,
        "Line": entity.get("Line") or [],
    }


def post_endpoint_for(txn_type: str, entity: dict) -> str:
    """Checks are Purchases with PaymentType=Check on most companies."""
    kind = (txn_type or "").lower()
    if kind == "check" or entity.get("PaymentType") == "Check":
        if kind == "check":
            return "/purchase"
    mapping = {
        "purchase": "/purchase",
        "deposit": "/deposit",
        "salesreceipt": "/salesreceipt",
        "journalentry": "/journalentry",
        "payment": "/payment",
        "bill": "/bill",
        "billpayment": "/billpayment",
        "transfer": "/transfer",
    }
    path = mapping.get(kind)
    if not path:
        raise WriteRefused(f"Write refused: no update endpoint for '{txn_type}'.")
    return path


def verified_status(confirmed: bool) -> str:
    """Success is verified. An unconfirmed post is failed, never applied."""
    return "verified" if confirmed else "failed"
