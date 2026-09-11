"""Nurse payout release: duplicates, failures, pending states and retries.

    python -m unittest tests.test_payout_release -v

Runs offline. Only third-party packages are stubbed (see _offline_stubs);
`app.services.payout_service` itself is the real module, so the state machine
under test is the one that ships.

The rule these tests exist to defend:

    A payout is marked `paid` if and only if Razorpay has reported a terminal
    success for it.

Everything else — an accepted request, a queued transfer, a timeout, a
mock-mode response — must leave the payout un-paid, because a marketplace
that treats "we sent the request" as "the nurse was paid" either pays twice
or tells a nurse she has money she does not have.
"""
from __future__ import annotations

import logging
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4

from tests import _offline_stubs

_offline_stubs.install()

from tests._offline_stubs import FakeResult, FakeSession  # noqa: E402

from app.models.enums import PayoutApprovalStatus, WorkerPayoutStatus  # noqa: E402
from app.services import payout_service  # noqa: E402
from app.services.payout_service import (  # noqa: E402
    _apply_razorpay_status,
    payout_idempotency_seed,
    release_payout,
    sync_payout_status,
)

D = Decimal

# Several tests deliberately drive Razorpay into timeouts and rejections. The
# service logs those with full tracebacks, which is correct in production and
# pure noise here.
logging.getLogger("app.services.payout_service").setLevel(logging.CRITICAL)


def make_payout(**overrides):
    """A payout row in the state the admin queue hands to Release Payment:
    approved, unpaid, nothing sent to Razorpay yet."""
    defaults = dict(
        id=uuid4(),
        worker_id=uuid4(),
        booking_id=uuid4(),
        gross_amount=D("620.00"),
        tds_deducted=D("0.00"),
        net_amount=D("430.00"),
        status=WorkerPayoutStatus.pending,
        approval_status=PayoutApprovalStatus.approved,
        attempt_count=0,
        max_attempts=3,
        razorpay_payout_id=None,
        razorpay_payout_status=None,
        razorpay_utr=None,
        razorpay_fund_account_id=None,
        razorpay_last_response=None,
        idempotency_key=None,
        paid_at=None,
        released_at=None,
        released_by=None,
        failure_reason=None,
        failure_code=None,
        next_retry_at=None,
        last_status_checked_at=None,
        ready_for_release_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def make_worker(with_bank: bool = True):
    return SimpleNamespace(
        id=uuid4(),
        user_id=uuid4(),
        bank_account_holder="Kavitha Rani",
        bank_account_number="1234567890" if with_bank else None,
        bank_ifsc="HDFC0001234" if with_bank else None,
        razorpay_fund_account_id="fa_existing_123" if with_bank else None,
        razorpay_contact_id="cont_existing_123" if with_bank else None,
    )


class FakeRazorpay:
    """Scriptable RazorpayX double.

    Records every payout request so the tests can assert on how many
    transfers were actually attempted — which is the whole question in the
    duplicate cases.
    """

    TERMINAL_SUCCESS = {"processed"}
    TERMINAL_FAILURE = {"failed", "cancelled", "reversed", "rejected"}

    def __init__(self, *, enabled=True, create_response=None, create_error=None,
                 fetch_response=None, fetch_error=None):
        self.payouts_enabled = enabled
        self._create_response = create_response or {
            "id": "pout_TEST123", "status": "processed", "utr": "UTR999"
        }
        self._create_error = create_error
        self._fetch_response = fetch_response
        self._fetch_error = fetch_error
        self.create_calls = []
        self.fetch_calls = []

    async def initiate_payout(self, fund_account_id, amount_paise, reference,
                              notes=None, idempotency_key=None, mode=None):
        self.create_calls.append({
            "fund_account_id": fund_account_id,
            "amount_paise": amount_paise,
            "reference": reference,
            "idempotency_key": idempotency_key,
        })
        if self._create_error:
            raise self._create_error
        return self._create_response

    async def fetch_payout(self, payout_id):
        self.fetch_calls.append(payout_id)
        if self._fetch_error:
            raise self._fetch_error
        return self._fetch_response or {"id": payout_id, "status": "processed",
                                        "utr": "UTR_FETCHED"}

    async def create_fund_account(self, **kwargs):
        return {"contact_id": "cont_new", "fund_account_id": "fa_new"}


async def run_release(payout, worker=None, razorpay=None, session=None):
    """Drive release_payout with a fake session that serves the worker row."""
    session = session or FakeSession()
    session.queue(FakeResult(worker if worker is not None else make_worker()))
    rp = razorpay or FakeRazorpay()
    with patch.object(payout_service, "razorpay_client", rp):
        result = await release_payout(session, payout, released_by=uuid4())
    return result, rp, session


# ===========================================================================
# The core safety rule
# ===========================================================================
class TestRazorpayStatusMapping(unittest.IsolatedAsyncioTestCase):
    """_apply_razorpay_status is the ONLY place `paid` may be set."""

    def test_processed_is_the_only_status_that_pays(self):
        payout = make_payout()
        with patch.object(payout_service, "razorpay_client", FakeRazorpay()):
            _apply_razorpay_status(payout, {"id": "pout_1", "status": "processed",
                                            "utr": "UTR123"})
        self.assertEqual(payout.status, WorkerPayoutStatus.paid)
        self.assertEqual(payout.razorpay_utr, "UTR123")
        self.assertIsNotNone(payout.paid_at)

    def test_non_terminal_statuses_never_pay(self):
        for status in ("queued", "pending", "processing", "scheduled", ""):
            payout = make_payout()
            with patch.object(payout_service, "razorpay_client", FakeRazorpay()):
                _apply_razorpay_status(payout, {"id": "pout_1", "status": status})
            self.assertEqual(
                payout.status, WorkerPayoutStatus.processing,
                f"status={status!r} must not resolve to paid",
            )
            self.assertIsNone(payout.paid_at, f"paid_at set for status={status!r}")

    def test_terminal_failures_mark_failed(self):
        for status in ("failed", "cancelled", "reversed", "rejected"):
            payout = make_payout()
            with patch.object(payout_service, "razorpay_client", FakeRazorpay()):
                _apply_razorpay_status(payout, {
                    "id": "pout_1", "status": status,
                    "failure_reason": "Beneficiary account invalid",
                })
            self.assertEqual(payout.status, WorkerPayoutStatus.failed)
            self.assertIsNone(payout.paid_at)
            self.assertEqual(payout.failure_code, status)

    def test_raw_response_is_retained_for_reconciliation(self):
        payout = make_payout()
        body = {"id": "pout_9", "status": "processed", "utr": "U1", "fees": 590}
        with patch.object(payout_service, "razorpay_client", FakeRazorpay()):
            _apply_razorpay_status(payout, body)
        self.assertEqual(payout.razorpay_last_response, body)
        self.assertIsNotNone(payout.last_status_checked_at)

    def test_a_later_success_clears_an_earlier_failure(self):
        """A retry that succeeds must not leave a stale failure reason on the
        row — support reads that field to decide whether to intervene."""
        payout = make_payout(status=WorkerPayoutStatus.failed,
                             failure_reason="Bank down", failure_code="failed")
        with patch.object(payout_service, "razorpay_client", FakeRazorpay()):
            _apply_razorpay_status(payout, {"id": "p", "status": "processed",
                                            "utr": "U2"})
        self.assertEqual(payout.status, WorkerPayoutStatus.paid)
        self.assertIsNone(payout.failure_reason)
        self.assertIsNone(payout.failure_code)


# ===========================================================================
# Duplicate prevention
# ===========================================================================
class TestDuplicatePrevention(unittest.IsolatedAsyncioTestCase):
    async def test_already_paid_payout_is_never_re_sent(self):
        payout = make_payout(
            status=WorkerPayoutStatus.paid,
            razorpay_payout_id="pout_DONE",
            razorpay_utr="UTR_DONE",
            paid_at=datetime.now(timezone.utc),
        )
        result, rp, _ = await run_release(payout)
        self.assertTrue(result["already_released"])
        self.assertEqual(result["status"], "paid")
        self.assertEqual(rp.create_calls, [], "a paid payout was sent to Razorpay again")

    async def test_double_click_sends_exactly_one_transfer(self):
        """Two Release clicks in a row: the second must poll, not re-create."""
        payout = make_payout()
        rp = FakeRazorpay()

        first, _, _ = await run_release(payout, razorpay=rp)
        self.assertEqual(first["status"], "paid")
        self.assertEqual(len(rp.create_calls), 1)

        second, _, _ = await run_release(payout, razorpay=rp)
        self.assertTrue(second["already_released"])
        self.assertEqual(len(rp.create_calls), 1,
                         "second click created a second transfer")

    async def test_in_flight_payout_is_polled_not_recreated(self):
        """A payout already at Razorpay but unconfirmed must be polled."""
        payout = make_payout(
            status=WorkerPayoutStatus.processing,
            razorpay_payout_id="pout_INFLIGHT",
            razorpay_payout_status="queued",
        )
        rp = FakeRazorpay(fetch_response={"id": "pout_INFLIGHT",
                                          "status": "processed", "utr": "UTR_X"})
        result, _, _ = await run_release(payout, razorpay=rp)

        self.assertTrue(result["polled_existing"])
        self.assertEqual(rp.create_calls, [], "in-flight payout was re-sent")
        self.assertEqual(rp.fetch_calls, ["pout_INFLIGHT"])
        self.assertEqual(payout.status, WorkerPayoutStatus.paid)

    async def test_idempotency_key_is_stable_across_retries(self):
        """The key must be identical on every attempt, or Razorpay would treat
        a retry as a brand-new transfer."""
        payout = make_payout()
        rp = FakeRazorpay(create_response={"id": "p1", "status": "queued"})

        await run_release(payout, razorpay=rp)
        first_key = rp.create_calls[0]["idempotency_key"]

        payout.status = WorkerPayoutStatus.failed
        payout.razorpay_payout_id = None
        await run_release(payout, razorpay=rp)
        second_key = rp.create_calls[1]["idempotency_key"]

        self.assertEqual(first_key, second_key)
        self.assertTrue(first_key.startswith("payout_"))

    def test_idempotency_seed_is_derived_from_the_booking(self):
        """Derived, not random: two payout rows for one booking (which the
        unique index prevents, but belt and braces) still collide at Razorpay
        rather than paying twice."""
        booking_id = UUID("11111111-2222-3333-4444-555555555555")
        self.assertEqual(payout_idempotency_seed(booking_id),
                         payout_idempotency_seed(booking_id))
        self.assertNotEqual(payout_idempotency_seed(booking_id),
                            payout_idempotency_seed(uuid4()))


# ===========================================================================
# Guards before any money moves
# ===========================================================================
class TestReleaseGuards(unittest.IsolatedAsyncioTestCase):
    async def test_unapproved_payout_is_refused(self):
        payout = make_payout(approval_status=PayoutApprovalStatus.pending_approval)
        result, rp, _ = await run_release(payout)
        self.assertIn("approved", result["error"].lower())
        self.assertEqual(rp.create_calls, [])

    async def test_rejected_payout_is_refused(self):
        payout = make_payout(approval_status=PayoutApprovalStatus.rejected)
        result, rp, _ = await run_release(payout)
        self.assertIn("error", result)
        self.assertEqual(rp.create_calls, [])

    async def test_held_payout_is_refused(self):
        payout = make_payout(status=WorkerPayoutStatus.on_hold)
        result, rp, _ = await run_release(payout)
        self.assertEqual(result["status"], "on_hold")
        self.assertEqual(rp.create_calls, [])

    async def test_zero_amount_payout_is_refused(self):
        payout = make_payout(net_amount=D("0.00"))
        result, rp, _ = await run_release(payout)
        self.assertIn("zero", result["error"].lower())
        self.assertEqual(rp.create_calls, [])

    async def test_missing_bank_details_fail_without_calling_razorpay(self):
        payout = make_payout()
        worker = make_worker(with_bank=False)
        result, rp, _ = await run_release(payout, worker=worker)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(payout.failure_code, "missing_bank_details")
        self.assertTrue(result["retryable"])
        self.assertEqual(rp.create_calls, [])

    async def test_retry_limit_stops_further_attempts(self):
        payout = make_payout(status=WorkerPayoutStatus.failed,
                             attempt_count=3, max_attempts=3)
        result, rp, _ = await run_release(payout)
        self.assertIn("retry limit", result["error"].lower())
        self.assertEqual(rp.create_calls, [])

    async def test_amount_sent_is_the_net_amount_in_paise(self):
        payout = make_payout(net_amount=D("430.00"))
        _, rp, _ = await run_release(payout)
        self.assertEqual(rp.create_calls[0]["amount_paise"], 43000)

    async def test_paise_conversion_does_not_lose_a_rupee(self):
        payout = make_payout(net_amount=D("1234.56"))
        _, rp, _ = await run_release(payout)
        self.assertEqual(rp.create_calls[0]["amount_paise"], 123456)


# ===========================================================================
# Pending / failure / retry
# ===========================================================================
class TestPendingAndFailure(unittest.IsolatedAsyncioTestCase):
    async def test_queued_response_leaves_payout_processing(self):
        payout = make_payout()
        rp = FakeRazorpay(create_response={"id": "pout_Q", "status": "queued"})
        result, _, _ = await run_release(payout, razorpay=rp)

        self.assertEqual(payout.status, WorkerPayoutStatus.processing)
        self.assertIsNone(payout.paid_at)
        self.assertEqual(result["razorpay_status"], "queued")

    async def test_razorpay_rejection_marks_failed(self):
        payout = make_payout()
        rp = FakeRazorpay(create_response={
            "id": "pout_F", "status": "failed",
            "failure_reason": "Invalid IFSC",
        })
        await run_release(payout, razorpay=rp)
        self.assertEqual(payout.status, WorkerPayoutStatus.failed)
        self.assertIsNone(payout.paid_at)
        self.assertIn("Invalid IFSC", payout.failure_reason)

    async def test_network_timeout_leaves_processing_not_failed(self):
        """The critical ambiguous case.

        A timeout means we do not know whether Razorpay received the request.
        Marking it `failed` would invite a retry that creates a SECOND
        transfer. It stays `processing` so the next attempt polls instead.
        """
        payout = make_payout()
        rp = FakeRazorpay(create_error=TimeoutError("read timeout"))
        result, _, _ = await run_release(payout, razorpay=rp)

        self.assertEqual(payout.status, WorkerPayoutStatus.processing)
        self.assertIsNone(payout.paid_at)
        self.assertTrue(result["retryable"])
        self.assertEqual(payout.failure_code, "provider_error")

    async def test_retry_after_timeout_polls_by_idempotency_key(self):
        """Follow-on from the timeout case: the retry must not re-create."""
        payout = make_payout()
        rp = FakeRazorpay(create_error=TimeoutError("read timeout"))
        await run_release(payout, razorpay=rp)
        self.assertEqual(len(rp.create_calls), 1)

        # Razorpay did in fact receive it; the id arrives via webhook later.
        payout.razorpay_payout_id = "pout_LATE"
        rp2 = FakeRazorpay(fetch_response={"id": "pout_LATE", "status": "processed",
                                           "utr": "UTR_LATE"})
        result, _, _ = await run_release(payout, razorpay=rp2)

        self.assertEqual(rp2.create_calls, [], "retry created a duplicate transfer")
        self.assertTrue(result["polled_existing"])
        self.assertEqual(payout.status, WorkerPayoutStatus.paid)

    async def test_failed_payout_can_be_retried(self):
        payout = make_payout(status=WorkerPayoutStatus.failed, attempt_count=1,
                             failure_reason="Bank unreachable")
        rp = FakeRazorpay(create_response={"id": "pout_R", "status": "processed",
                                           "utr": "UTR_R"})
        result, _, _ = await run_release(payout, razorpay=rp)
        self.assertEqual(payout.status, WorkerPayoutStatus.paid)
        self.assertEqual(payout.attempt_count, 2)
        self.assertEqual(result["utr"], "UTR_R")

    async def test_attempt_count_increments_on_every_real_attempt(self):
        payout = make_payout()
        rp = FakeRazorpay(create_response={"id": "p", "status": "queued"})
        await run_release(payout, razorpay=rp)
        self.assertEqual(payout.attempt_count, 1)

        payout.razorpay_payout_id = None
        payout.status = WorkerPayoutStatus.failed
        await run_release(payout, razorpay=rp)
        self.assertEqual(payout.attempt_count, 2)

    async def test_poll_failure_is_reported_without_changing_state(self):
        """If we cannot reach Razorpay to confirm, the payout must keep its
        current state rather than being guessed either way."""
        from app.integrations.providers import ExternalProviderError

        payout = make_payout(status=WorkerPayoutStatus.processing,
                             razorpay_payout_id="pout_UNK")
        rp = FakeRazorpay(fetch_error=ExternalProviderError("gateway 503"))
        result, _, _ = await run_release(payout, razorpay=rp)

        self.assertEqual(payout.status, WorkerPayoutStatus.processing)
        self.assertIsNone(payout.paid_at)
        self.assertTrue(result["retryable"])


# ===========================================================================
# Manual settlement (no RazorpayX configured)
# ===========================================================================
class TestManualSettlement(unittest.IsolatedAsyncioTestCase):
    async def test_records_manual_settlement_when_razorpayx_is_off(self):
        payout = make_payout()
        rp = FakeRazorpay(enabled=False)
        result, _, _ = await run_release(payout, razorpay=rp)

        self.assertTrue(result["manual"])
        self.assertEqual(payout.status, WorkerPayoutStatus.paid)
        self.assertEqual(rp.create_calls, [])
        # Flagged explicitly so it is never mistaken for a bank-confirmed
        # transfer during reconciliation.
        self.assertEqual(payout.razorpay_payout_status, "manual_settlement")
        self.assertIsNone(payout.razorpay_utr)

    async def test_manual_settlement_is_still_duplicate_protected(self):
        payout = make_payout()
        rp = FakeRazorpay(enabled=False)
        await run_release(payout, razorpay=rp)
        second, _, _ = await run_release(payout, razorpay=rp)
        self.assertTrue(second["already_released"])


# ===========================================================================
# Status reconciliation (missed webhook safety net)
# ===========================================================================
class TestSyncStatus(unittest.IsolatedAsyncioTestCase):
    async def test_sync_resolves_a_stuck_processing_payout(self):
        payout = make_payout(status=WorkerPayoutStatus.processing,
                             razorpay_payout_id="pout_STUCK",
                             razorpay_payout_status="queued")
        rp = FakeRazorpay(fetch_response={"id": "pout_STUCK", "status": "processed",
                                          "utr": "UTR_SYNCED"})
        with patch.object(payout_service, "razorpay_client", rp):
            result = await sync_payout_status(FakeSession(), payout)

        self.assertEqual(payout.status, WorkerPayoutStatus.paid)
        self.assertEqual(result["utr"], "UTR_SYNCED")

    async def test_sync_reports_a_terminal_failure(self):
        payout = make_payout(status=WorkerPayoutStatus.processing,
                             razorpay_payout_id="pout_REV")
        rp = FakeRazorpay(fetch_response={"id": "pout_REV", "status": "reversed",
                                          "failure_reason": "Account closed"})
        with patch.object(payout_service, "razorpay_client", rp):
            await sync_payout_status(FakeSession(), payout)

        self.assertEqual(payout.status, WorkerPayoutStatus.failed)
        self.assertIsNone(payout.paid_at)

    async def test_sync_without_a_razorpay_id_is_a_no_op(self):
        payout = make_payout()
        rp = FakeRazorpay()
        with patch.object(payout_service, "razorpay_client", rp):
            result = await sync_payout_status(FakeSession(), payout)
        self.assertIn("error", result)
        self.assertEqual(rp.fetch_calls, [])

    async def test_sync_on_a_paid_payout_does_not_re_query(self):
        payout = make_payout(status=WorkerPayoutStatus.paid,
                             razorpay_payout_id="pout_P", razorpay_utr="U")
        rp = FakeRazorpay()
        with patch.object(payout_service, "razorpay_client", rp):
            result = await sync_payout_status(FakeSession(), payout)
        self.assertEqual(result["status"], "paid")
        self.assertEqual(rp.fetch_calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
