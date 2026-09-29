"""Controlled QBO tools.

Read tools accept an allowlisted entity name. They do not accept raw endpoints.
Write tools accept a WritePermit and nothing else can reach QBOClient._post
with a valid permit except the guard's callers.
"""
import re

from app.bookkeeper.write_guard import WRITE_PATHS, WritePermit, WriteRefused, normalize_endpoint
from app.qbo_client import QBOClient

READ_ENTITIES = frozenset({
    "Account",
    "Customer",
    "Vendor",
    "Invoice",
    "Payment",
    "SalesReceipt",
    "RefundReceipt",
    "Bill",
    "BillPayment",
    "VendorCredit",
    "Purchase",
    "Deposit",
    "Transfer",
    "JournalEntry",
    "CreditCardPayment",
    "TaxCode",
    "Employee",
})

_WHERE_REJECT = re.compile(r"(;|--|/\*|\bSTARTPOSITION\b|\bMAXRESULTS\b|\bSELECT\b)", re.IGNORECASE)


class QboTools:
    def __init__(self, company):
        if not getattr(company, "realm_id", None):
            raise ValueError("realm_id is required")
        self.realm_id = company.realm_id
        self._client = QBOClient(company)

    def query_entity(self, db, entity: str, where: str = "") -> tuple[list, bool, int]:
        if entity not in READ_ENTITIES:
            raise WriteRefused(f"Read refused: '{entity}' is not an allowlisted entity.")
        clause = (where or "").strip()
        if clause and _WHERE_REJECT.search(clause):
            raise WriteRefused("Read refused: the filter is not a simple WHERE clause.")
        sql = f"SELECT * FROM {entity}"
        if clause:
            sql += f" WHERE {clause}"
        return self._client._query_all(db, sql)

    def get_entity(self, db, entity: str, entity_id: str) -> dict:
        if entity not in READ_ENTITIES:
            raise WriteRefused(f"Read refused: '{entity}' is not an allowlisted entity.")
        if not entity_id or not str(entity_id).isalnum():
            raise WriteRefused("Read refused: entity id is missing or not a QBO id.")
        return self._client.get_transaction(db, entity, str(entity_id))

    def post_update(self, db, permit: WritePermit, endpoint: str, payload: dict) -> dict:
        path = normalize_endpoint(endpoint)
        if permit is None or permit.realm_id != self.realm_id or permit.endpoint != path:
            raise WriteRefused("Write refused: permit does not match this company and endpoint.")
        if path not in WRITE_PATHS:
            raise WriteRefused(f"Write refused: '{path}' is not allowlisted.")
        return self._client._post(db, path, payload, permit=permit)
