"""Guards for the bookkeeper: no cross-company rules, no unapproved writes, no truncated pages."""
import unittest
from datetime import datetime
from types import SimpleNamespace

from app.bookkeeper.history import build_payee_history, suggest_from_history
from app.bookkeeper.knowledge import focus_for
from app.bookkeeper.pagination import walk_pages
from app.bookkeeper.review import review_statements
from app.bookkeeper.write_guard import (
    WriteRefused,
    account_is_set,
    issue_permit,
    patch_first_account,
    unwrap_entity,
    verified_status,
)
from app.qbo_client import QBOClient


def _uncategorized(name: str) -> bool:
    return "uncategorized" in (name or "").lower()


class HistoryTests(unittest.TestCase):
    def test_suggestion_uses_only_the_transactions_it_is_given(self):
        company_a = [("Purchase", {
            "EntityRef": {"name": "Sysco"},
            "Line": [{"AccountBasedExpenseLineDetail": {"AccountRef": {"value": "10", "name": "Food"}}}],
        })] * 8
        company_b = [("Purchase", {
            "EntityRef": {"name": "Sysco"},
            "Line": [{"AccountBasedExpenseLineDetail": {"AccountRef": {"value": "99", "name": "Inventory"}}}],
        })] * 8
        suggestion_a = suggest_from_history(build_payee_history(company_a, _uncategorized), "Sysco")
        suggestion_b = suggest_from_history(build_payee_history(company_b, _uncategorized), "Sysco")
        self.assertEqual(suggestion_a.account_name, "Food")
        self.assertEqual(suggestion_b.account_name, "Inventory")
        self.assertIsNone(suggest_from_history({}, "Sysco"))

    def test_thin_history_is_not_a_proposal(self):
        rows = [("Purchase", {
            "EntityRef": {"name": "Amazon"},
            "Line": [{"AccountBasedExpenseLineDetail": {"AccountRef": {"value": "1", "name": "Supplies"}}}],
        })]
        self.assertIsNone(suggest_from_history(build_payee_history(rows, _uncategorized), "Amazon"))


class KnowledgeTests(unittest.TestCase):
    def test_business_focus_has_no_account_mapping(self):
        for industry in ("restaurant", "retail", "nonprofit", "construction", "consulting", ""):
            _kind, focus = focus_for(industry)
            blob = " ".join(focus).lower()
            self.assertNotIn("sysco", blob)
            self.assertNotIn("office supplies", blob)


class WriteGuardTests(unittest.TestCase):
    def test_unapproved_and_cross_company_are_refused(self):
        with self.assertRaises(WriteRefused):
            issue_permit(
                realm_id="A",
                record_realm_id="A",
                status="pending",
                action_id="1",
                endpoint="/purchase",
            )
        with self.assertRaises(WriteRefused):
            issue_permit(
                realm_id="A",
                record_realm_id="B",
                status="approved",
                action_id="1",
                endpoint="/purchase",
            )

    def test_post_without_permit_never_calls_qbo(self):
        client = QBOClient(SimpleNamespace(realm_id="abc", id="1", qbo_environment="sandbox"))
        with self.assertRaises(WriteRefused):
            client._post(None, "/journalentry", {"Line": []})

    def test_patch_and_verify_use_the_entity_not_the_envelope(self):
        envelope = {"time": "t", "Purchase": {
            "Id": "9",
            "SyncToken": "0",
            "Line": [{"Amount": 10, "AccountBasedExpenseLineDetail": {"AccountRef": {"value": "1", "name": "Old"}}}],
        }}
        entity = unwrap_entity(envelope, "Purchase")
        self.assertTrue(patch_first_account(entity, "55", "Office Supplies"))
        self.assertTrue(account_is_set(entity, "55"))
        self.assertFalse(account_is_set(envelope, "55"))
        self.assertEqual(verified_status(True), "verified")
        self.assertEqual(verified_status(False), "failed")


class PaginationTests(unittest.TestCase):
    def test_short_page_is_complete_and_duplicates_drop(self):
        pages = {
            1: [{"Id": "1"}, {"Id": "2"}],
            3: [{"Id": "2"}, {"Id": "3"}],
        }

        def fetch(start, size):
            return pages.get(start, [])

        rows, complete, total = walk_pages(fetch, page_size=2)
        self.assertTrue(complete)
        self.assertEqual([row["Id"] for row in rows], ["1", "2", "3"])
        self.assertEqual(total, 3)

    def test_endless_full_pages_are_incomplete(self):
        def fetch(start, size):
            return [{"Id": str(start + i)} for i in range(size)]

        _rows, complete, _total = walk_pages(fetch, page_size=1)
        self.assertFalse(complete)


class StatementTests(unittest.TestCase):
    def test_unbalanced_cached_statement_is_critical_and_labeled_cached(self):
        def data_row(label, value):
            return {"type": "Data", "ColData": [{"value": label, "id": label}, {"value": value}]}

        profile = SimpleNamespace(
            data_as_of=datetime.utcnow(),
            balance_sheet_data={"Rows": {"Row": [{"type": "Section", "Rows": {"Row": [
                data_row("Total Assets", "100"),
                data_row("Total Liabilities", "40"),
                data_row("Total Equity", "40"),
            ]}}]}},
            pl_data={"Rows": {"Row": [{"type": "Section", "Rows": {"Row": [
                data_row("Net Income", "5"),
            ]}}]}},
            ar_aging_data={},
            ap_aging_data={},
        )
        company = SimpleNamespace(id="c1", realm_id="realm-1")

        class _Query:
            def filter(self, *_args, **_kwargs):
                return self

            def all(self):
                return []

        class _Db:
            def __init__(self):
                self.added = []

            def query(self, *_args):
                return _Query()

            def add(self, obj):
                self.added.append(obj)

        db = _Db()
        result = review_statements(db, company, profile)
        self.assertEqual(result["freshness"], "CACHED")
        self.assertEqual(result["source"], "CACHED_SYNC")
        self.assertTrue(any(item["severity"] == "CRITICAL" for item in result["findings"]))
        self.assertEqual(db.added[0].realm_id, "realm-1")
        self.assertEqual(db.added[0].autonomy_level, 3)


if __name__ == "__main__":
    unittest.main()
