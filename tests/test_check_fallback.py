"""
Tests for get_checks_with_fallback() — Phase 1 fix for Intuit 4001
"invalid context declaration: Check" pattern.

Four cases:
  1. Check + 4001 + "invalid context declaration" → fallback fires
  2. Check + 4001 + different detail (real SQL bug) → raises, no fallback
  3. Check + 4000 entity_not_supported → fallback fires (existing behavior)
  4. Check + 401 auth error → raises, no fallback (existing behavior)
"""
import json
import unittest
from unittest.mock import MagicMock, patch, call

import requests

# ---------------------------------------------------------------------------
# Helpers — build fake HTTPError responses
# ---------------------------------------------------------------------------

def _make_http_error(status: int, intuit_code: str = "", message: str = "", detail: str = "") -> requests.exceptions.HTTPError:
    """Build a requests.exceptions.HTTPError with a fake response."""
    body = {}
    if intuit_code or message or detail:
        body = {
            "Fault": {
                "Error": [
                    {
                        "code": intuit_code,
                        "Message": message,
                        "Detail": detail,
                    }
                ]
            }
        }

    fake_response = MagicMock()
    fake_response.status_code = status
    fake_response.json.return_value = body
    fake_response.headers = {}

    err = requests.exceptions.HTTPError(response=fake_response)
    err.response = fake_response
    return err


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestGetChecksWithFallback(unittest.TestCase):

    def _make_client(self):
        """Return a QBOClient with a mocked company object (no real DB/tokens)."""
        from app.qbo_client import QBOClient
        company = MagicMock()
        company.qbo_environment = "production"
        company.access_token_enc = "enc_token"
        company.refresh_token_enc = "enc_refresh"
        company.token_expires_at = None
        client = QBOClient.__new__(QBOClient)
        client.company = company
        client.environment = "production"
        return client

    # ------------------------------------------------------------------
    # Test 1 — Check + 4001 "invalid context declaration" → fallback fires
    # ------------------------------------------------------------------
    def test_check_4001_invalid_context_declaration_triggers_fallback(self):
        """
        Production behavior observed on realm 193514338943022:
        Intuit returns 4001 QueryValidationError: invalid context declaration: Check.
        classify_qbo_error() → malformed_query (correct).
        get_checks_with_fallback() MUST treat this as entity unavailability
        and activate the Purchase(PaymentType='Check') fallback.
        """
        client = self._make_client()
        db = MagicMock()

        fallback_records = [{"Id": "P1", "PaymentType": "Check", "TxnDate": "2026-01-15"}]

        check_error = _make_http_error(
            status=400,
            intuit_code="4001",
            message="Invalid query",
            detail="QueryValidationError: invalid context declaration: Check",
        )

        def _query_all_side_effect(db, sql):
            if "FROM Check" in sql:
                raise check_error
            # fallback: Purchase WHERE PaymentType = 'Check'
            return fallback_records, True, len(fallback_records)

        with patch.object(client, "_query_all", side_effect=_query_all_side_effect) as mock_qa:
            records, info = client.get_checks_with_fallback(db, start_date="2026-01-01")

        # Fallback fired
        self.assertTrue(info["used_fallback"], "Fallback must have been used")
        self.assertEqual(info["entity_used"], "Purchase_Check")
        self.assertEqual(records, fallback_records)
        self.assertEqual(info["total_fetched"], 1)
        self.assertTrue(info["is_complete"])

        # First call was Check, second was fallback Purchase
        self.assertEqual(mock_qa.call_count, 2)
        first_sql = mock_qa.call_args_list[0][0][1]
        second_sql = mock_qa.call_args_list[1][0][1]
        self.assertIn("FROM Check", first_sql)
        self.assertIn("FROM Purchase", second_sql)
        self.assertIn("PaymentType = 'Check'", second_sql)

        # fallback_reason must carry the original error detail
        self.assertIn("invalid context declaration", info["fallback_reason"].lower())

    # ------------------------------------------------------------------
    # Test 2 — Check + 4001 + different detail → raises, no fallback
    # ------------------------------------------------------------------
    def test_check_4001_real_sql_bug_propagates(self):
        """
        A genuine malformed query against Check (e.g. bad field name) must NOT
        trigger the fallback.  The error must propagate to the caller unchanged
        so it surfaces as api_error / malformed_query for investigation.
        """
        client = self._make_client()
        db = MagicMock()

        sql_bug_error = _make_http_error(
            status=400,
            intuit_code="4001",
            message="Invalid query",
            detail="QueryValidationError: invalid field name: TxnDateBAD",
        )

        with patch.object(client, "_query_all", side_effect=sql_bug_error) as mock_qa:
            with self.assertRaises(requests.exceptions.HTTPError) as ctx:
                client.get_checks_with_fallback(db, start_date="2026-01-01")

        # Only one attempt — no fallback
        self.assertEqual(mock_qa.call_count, 1)
        # The raised exception is the original one
        self.assertIs(ctx.exception, sql_bug_error)

    # ------------------------------------------------------------------
    # Test 3 — Check + 4000 entity_not_supported → fallback fires (existing)
    # ------------------------------------------------------------------
    def test_check_4000_entity_not_supported_triggers_fallback(self):
        """
        Original Phase 1 path: Intuit returns error 4000 (entity not recognized).
        classify_qbo_error() → entity_not_supported.
        Fallback must fire exactly as it did before the fix.
        """
        client = self._make_client()
        db = MagicMock()

        fallback_records = [{"Id": "P2", "PaymentType": "Check", "TxnDate": "2026-02-10"}]

        entity_error = _make_http_error(
            status=400,
            intuit_code="4000",
            message="entity type not recognized",
            detail="Check is not a valid entity",
        )

        def _query_all_side_effect(db, sql):
            if "FROM Check" in sql:
                raise entity_error
            return fallback_records, True, len(fallback_records)

        with patch.object(client, "_query_all", side_effect=_query_all_side_effect) as mock_qa:
            records, info = client.get_checks_with_fallback(db, start_date="2026-01-01")

        self.assertTrue(info["used_fallback"])
        self.assertEqual(info["entity_used"], "Purchase_Check")
        self.assertEqual(records, fallback_records)
        self.assertEqual(mock_qa.call_count, 2)

    # ------------------------------------------------------------------
    # Test 4 — Check + 401 auth error → raises, no fallback
    # ------------------------------------------------------------------
    def test_check_auth_error_propagates(self):
        """
        Auth errors (401/403) are not entity-availability issues.
        They must propagate unchanged — no fallback, no swallowing.
        """
        client = self._make_client()
        db = MagicMock()

        auth_error = _make_http_error(status=401)

        with patch.object(client, "_query_all", side_effect=auth_error) as mock_qa:
            with self.assertRaises(requests.exceptions.HTTPError) as ctx:
                client.get_checks_with_fallback(db, start_date="2026-01-01")

        self.assertEqual(mock_qa.call_count, 1)
        self.assertIs(ctx.exception, auth_error)

    # ------------------------------------------------------------------
    # Bonus — case-insensitivity of the "invalid context declaration" check
    # ------------------------------------------------------------------
    def test_check_4001_context_declaration_case_insensitive(self):
        """
        The phrase match is case-insensitive so variations in Intuit's
        capitalisation don't break the guard.
        """
        client = self._make_client()
        db = MagicMock()

        fallback_records = [{"Id": "P3", "PaymentType": "Check"}]

        # Capitalised variant
        check_error = _make_http_error(
            status=400,
            intuit_code="4001",
            message="Invalid query",
            detail="QueryValidationError: Invalid Context Declaration: Check",
        )

        def _query_all_side_effect(db, sql):
            if "FROM Check" in sql:
                raise check_error
            return fallback_records, True, 1

        with patch.object(client, "_query_all", side_effect=_query_all_side_effect):
            records, info = client.get_checks_with_fallback(db, start_date="2026-01-01")

        self.assertTrue(info["used_fallback"])
        self.assertEqual(records, fallback_records)


if __name__ == "__main__":
    unittest.main(verbosity=2)
