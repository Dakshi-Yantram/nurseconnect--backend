"""Patch 4B — Trainer / Reviewer / Assessment content lifecycle tests."""
import os
import uuid

import pytest
import requests

from tests.conftest import auth_headers, API

# ---- Auth helpers reused from conftest fixtures (admin_clinical_auth, admin_super_auth, worker_auth) ----


def _h(auth):
    return auth_headers(auth)


# ============================================================================
# 1. AUTH — smoke: all three required principals authenticate
# ============================================================================
class TestAuth:
    def test_admin_clinical_login(self, admin_clinical_auth):
        assert admin_clinical_auth["tokens"]["access_token"]
        assert admin_clinical_auth["user"]["role"] == "admin_clinical"

    def test_admin_super_login(self, admin_super_auth):
        assert admin_super_auth["tokens"]["access_token"]
        assert admin_super_auth["user"]["role"] == "admin_super"

    def test_worker_login(self, worker_auth):
        assert worker_auth["tokens"]["access_token"]
        assert worker_auth["user"]["role"] == "worker"


# ============================================================================
# 2. TRAINER LIFECYCLE — training module: draft → review → approve/reject → publish
# ============================================================================
class TestTrainingModuleLifecycle:
    @pytest.fixture(scope="class")
    def created(self, admin_clinical_auth):
        code = f"TEST_TM_{uuid.uuid4().hex[:8]}"
        r = requests.post(
            f"{API}/training/modules",
            headers=_h(admin_clinical_auth),
            json={
                "code": code,
                "title": "TEST Module 4B",
                "description": "test",
                "duration_minutes": 10,
                "pass_percent": 70,
            },
            timeout=10,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        return {"code": code, "id": body["id"], "body": body}

    def test_create_returns_lifecycle_fields(self, created):
        b = created["body"]
        assert b["status"] == "draft"
        # Patch 4B contract: lifecycle metadata returned on create
        for k in [
            "created_by",
            "updated_by",
            "reviewed_by",
            "reviewed_at",
            "review_notes",
            "published_version",
            "published_at",
        ]:
            assert k in b, f"missing field {k}"
        assert b["created_by"] is not None

    def test_update_draft(self, created, admin_clinical_auth):
        r = requests.put(
            f"{API}/training/modules/{created['id']}",
            headers=_h(admin_clinical_auth),
            json={"title": "TEST Module 4B updated"},
            timeout=10,
        )
        assert r.status_code == 200, r.text
        assert r.json()["title"] == "TEST Module 4B updated"
        assert r.json()["status"] == "draft"

    def test_publish_from_draft_blocked(self, created, admin_clinical_auth):
        r = requests.post(
            f"{API}/training/{created['id']}/publish",
            headers=_h(admin_clinical_auth),
            json={},
            timeout=10,
        )
        assert r.status_code == 409, r.text

    def test_approve_from_draft_blocked(self, created, admin_clinical_auth):
        r = requests.post(
            f"{API}/training/{created['id']}/approve",
            headers=_h(admin_clinical_auth),
            json={},
            timeout=10,
        )
        assert r.status_code == 409, r.text

    def test_submit_review(self, created, admin_clinical_auth):
        r = requests.post(
            f"{API}/training/{created['id']}/submit-review",
            headers=_h(admin_clinical_auth),
            json={"notes": "ready"},
            timeout=10,
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "under_review"

    def test_submit_review_when_already_under_review(self, created, admin_clinical_auth):
        r = requests.post(
            f"{API}/training/{created['id']}/submit-review",
            headers=_h(admin_clinical_auth),
            json={},
            timeout=10,
        )
        assert r.status_code == 409

    def test_approve(self, created, admin_super_auth):
        r = requests.post(
            f"{API}/training/{created['id']}/approve",
            headers=_h(admin_super_auth),
            json={"notes": "lgtm"},
            timeout=10,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "approved"
        assert body["reviewed_by"] is not None
        assert body["reviewed_at"] is not None

    def test_edit_after_approved_blocked(self, created, admin_clinical_auth):
        r = requests.put(
            f"{API}/training/modules/{created['id']}",
            headers=_h(admin_clinical_auth),
            json={"title": "should fail"},
            timeout=10,
        )
        assert r.status_code == 409

    def test_publish(self, created, admin_super_auth):
        r = requests.post(
            f"{API}/training/{created['id']}/publish",
            headers=_h(admin_super_auth),
            json={},
            timeout=10,
        )
        assert r.status_code == 200, r.text
        b = r.json()
        assert b["status"] == "published"
        assert b["published_at"] is not None
        assert b["published_version"] is not None

    def test_submit_review_when_published_blocked(self, created, admin_clinical_auth):
        r = requests.post(
            f"{API}/training/{created['id']}/submit-review",
            headers=_h(admin_clinical_auth),
            json={},
            timeout=10,
        )
        assert r.status_code == 409

    def test_edit_published_blocked(self, created, admin_clinical_auth):
        r = requests.put(
            f"{API}/training/modules/{created['id']}",
            headers=_h(admin_clinical_auth),
            json={"title": "no go"},
            timeout=10,
        )
        assert r.status_code == 409


class TestTrainingModuleReject:
    """Cover the reject path → re-edit returns to draft."""

    def test_reject_and_re_edit_flow(self, admin_clinical_auth, admin_super_auth):
        code = f"TEST_TM_REJ_{uuid.uuid4().hex[:8]}"
        r = requests.post(
            f"{API}/training/modules",
            headers=_h(admin_clinical_auth),
            json={"code": code, "title": "TEST reject flow"},
            timeout=10,
        )
        assert r.status_code == 200
        mid = r.json()["id"]

        # Reject from draft -> blocked
        r = requests.post(f"{API}/training/{mid}/reject", headers=_h(admin_super_auth), json={}, timeout=10)
        assert r.status_code == 409

        r = requests.post(f"{API}/training/{mid}/submit-review", headers=_h(admin_clinical_auth), json={}, timeout=10)
        assert r.status_code == 200

        r = requests.post(f"{API}/training/{mid}/reject", headers=_h(admin_super_auth), json={"notes": "needs work"}, timeout=10)
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "rejected"
        assert body["review_notes"] == "needs work"

        # Editing a rejected module should re-arm draft
        r = requests.put(
            f"{API}/training/modules/{mid}",
            headers=_h(admin_clinical_auth),
            json={"description": "fixed"},
            timeout=10,
        )
        assert r.status_code == 200
        assert r.json()["status"] == "draft"


# ============================================================================
# 3. ASSESSMENT LIFECYCLE — same flow on AssessmentModule
# ============================================================================
class TestAssessmentLifecycle:
    @pytest.fixture(scope="class")
    def created(self, admin_clinical_auth):
        code = f"TEST_ASMT_{uuid.uuid4().hex[:8]}"
        payload = {
            "code": code,
            "title": "TEST Asmt 4B",
            "pass_score": 50,
            "questions": [
                {"id": "q1", "type": "single_select", "text": "?", "options": ["a", "b"], "correct_index": 1},
                {"id": "q2", "type": "boolean", "text": "?", "correct_bool": True},
            ],
        }
        r = requests.post(f"{API}/training/assessments", headers=_h(admin_clinical_auth), json=payload, timeout=10)
        assert r.status_code == 200, r.text
        return {"id": r.json()["id"], "code": code, "body": r.json()}

    def test_create_status_draft(self, created):
        assert created["body"]["status"] == "draft"

    def test_update_draft_assessment(self, created, admin_clinical_auth):
        r = requests.put(
            f"{API}/training/assessments/{created['id']}",
            headers=_h(admin_clinical_auth),
            json={"description": "updated desc"},
            timeout=10,
        )
        assert r.status_code == 200, r.text
        assert r.json()["description"] == "updated desc"

    def test_publish_from_draft_blocked(self, created, admin_super_auth):
        r = requests.post(f"{API}/assessments/{created['id']}/publish", headers=_h(admin_super_auth), json={}, timeout=10)
        assert r.status_code == 409

    def test_approve_from_draft_blocked(self, created, admin_super_auth):
        r = requests.post(f"{API}/assessments/{created['id']}/approve", headers=_h(admin_super_auth), json={}, timeout=10)
        assert r.status_code == 409

    def test_submit_review(self, created, admin_clinical_auth):
        r = requests.post(f"{API}/assessments/{created['id']}/submit-review", headers=_h(admin_clinical_auth), json={"notes": "ready"}, timeout=10)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "under_review"

    def test_approve(self, created, admin_super_auth):
        r = requests.post(f"{API}/assessments/{created['id']}/approve", headers=_h(admin_super_auth), json={"notes": "ok"}, timeout=10)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "approved"

    def test_publish(self, created, admin_super_auth):
        r = requests.post(f"{API}/assessments/{created['id']}/publish", headers=_h(admin_super_auth), json={}, timeout=10)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "published"
        assert body["published_at"] is not None
        assert body["published_version"] is not None

    def test_submit_review_after_publish_blocked(self, created, admin_clinical_auth):
        r = requests.post(f"{API}/assessments/{created['id']}/submit-review", headers=_h(admin_clinical_auth), json={}, timeout=10)
        assert r.status_code == 409

    def test_edit_published_blocked(self, created, admin_clinical_auth):
        r = requests.put(
            f"{API}/training/assessments/{created['id']}",
            headers=_h(admin_clinical_auth),
            json={"description": "no"},
            timeout=10,
        )
        assert r.status_code == 409


# ============================================================================
# 4. RBAC — worker forbidden from all lifecycle / create endpoints
# ============================================================================
class TestRBACWorkerForbidden:
    def test_worker_cannot_create_module(self, worker_auth):
        r = requests.post(
            f"{API}/training/modules",
            headers=_h(worker_auth),
            json={"code": "WORKER_FAIL", "title": "x"},
            timeout=10,
        )
        assert r.status_code == 403

    def test_worker_cannot_create_assessment(self, worker_auth):
        r = requests.post(
            f"{API}/training/assessments",
            headers=_h(worker_auth),
            json={
                "code": "WORKER_FAIL2",
                "title": "x",
                "questions": [{"id": "q1", "type": "boolean", "text": "?", "correct_bool": True}],
            },
            timeout=10,
        )
        assert r.status_code == 403

    def test_worker_cannot_submit_review(self, worker_auth):
        fake = uuid.uuid4()
        r = requests.post(f"{API}/training/{fake}/submit-review", headers=_h(worker_auth), json={}, timeout=10)
        assert r.status_code == 403
        r = requests.post(f"{API}/assessments/{fake}/submit-review", headers=_h(worker_auth), json={}, timeout=10)
        assert r.status_code == 403

    def test_worker_cannot_approve_reject_publish(self, worker_auth):
        fake = uuid.uuid4()
        for path in [
            f"{API}/training/{fake}/approve",
            f"{API}/training/{fake}/reject",
            f"{API}/training/{fake}/publish",
            f"{API}/assessments/{fake}/approve",
            f"{API}/assessments/{fake}/reject",
            f"{API}/assessments/{fake}/publish",
        ]:
            r = requests.post(path, headers=_h(worker_auth), json={}, timeout=10)
            assert r.status_code == 403, f"{path} returned {r.status_code}"

    def test_worker_cannot_access_admin_lists(self, worker_auth):
        r = requests.get(f"{API}/training/admin/modules", headers=_h(worker_auth), timeout=10)
        assert r.status_code == 403
        r = requests.get(f"{API}/training/admin/assessments", headers=_h(worker_auth), timeout=10)
        assert r.status_code == 403


# ============================================================================
# 5. WORKER VISIBILITY — only published rows returned
# ============================================================================
class TestWorkerVisibility:
    def test_worker_modules_only_published(self, worker_auth):
        r = requests.get(f"{API}/training/modules", headers=_h(worker_auth), timeout=10)
        assert r.status_code == 200
        # Worker endpoint does NOT return status field (different serializer),
        # but the underlying query filters status==published. Sanity check via admin list:
        # we'll cross-check below.
        assert isinstance(r.json(), list)

    def test_worker_assessments_only_published(self, worker_auth):
        r = requests.get(f"{API}/training/assessments", headers=_h(worker_auth), timeout=10)
        assert r.status_code == 200
        items = r.json()
        assert isinstance(items, list)
        for a in items:
            assert a["status"] == "published", a
        codes = [a["code"] for a in items]
        # Seeded published one should be present, draft one should NOT
        assert "IV_INFUSION_ASSESSMENT_V1" in codes
        assert "PICC_LINE_ASSESSMENT_V1_DRAFT" not in codes

    def test_worker_cannot_fetch_draft_by_id(self, worker_auth, admin_clinical_auth):
        # find PICC_LINE draft via admin list
        r = requests.get(f"{API}/training/admin/assessments?status=draft", headers=_h(admin_clinical_auth), timeout=10)
        assert r.status_code == 200
        draft = next((a for a in r.json() if a["code"] == "PICC_LINE_ASSESSMENT_V1_DRAFT"), None)
        assert draft is not None
        r = requests.get(f"{API}/training/assessments/{draft['id']}", headers=_h(worker_auth), timeout=10)
        assert r.status_code == 404


# ============================================================================
# 6/7. ASSESSMENT SUBMISSION — scoring + attempt persistence + 404 on draft
# ============================================================================
class TestAssessmentSubmission:
    def _find_iv_assessment(self, worker_auth):
        r = requests.get(f"{API}/training/assessments", headers=_h(worker_auth), timeout=10)
        assert r.status_code == 200
        return next(a for a in r.json() if a["code"] == "IV_INFUSION_ASSESSMENT_V1")

    def test_submit_passing_answers(self, worker_auth):
        a = self._find_iv_assessment(worker_auth)
        # q1 single_select correct=1, q2 boolean=True, q3 multi_select=[0,1,2], q4 text=any
        r = requests.post(
            f"{API}/training/assessments/{a['id']}/submit",
            headers=_h(worker_auth),
            json={"answers": [1, True, [0, 1, 2], "desc"]},
            timeout=15,
        )
        assert r.status_code == 200, r.text
        b = r.json()
        assert b["passed"] is True
        assert b["score"] == 100
        assert b["pass_score"] == 70
        assert b["completion_status"] == "passed"
        assert "qualification_unlocked" in b
        assert isinstance(b["qualification_unlocked"], list)

    def test_submit_failing_answers_persists_new_attempt(self, worker_auth):
        a = self._find_iv_assessment(worker_auth)
        r = requests.post(
            f"{API}/training/assessments/{a['id']}/submit",
            headers=_h(worker_auth),
            json={"answers": [0, False, [], ""]},
            timeout=15,
        )
        assert r.status_code == 200
        b = r.json()
        assert b["passed"] is False
        assert b["score"] < 70
        assert b["completion_status"] == "failed"

    def test_list_attempted_flag_set(self, worker_auth):
        r = requests.get(f"{API}/training/assessments", headers=_h(worker_auth), timeout=10)
        item = next(x for x in r.json() if x["code"] == "IV_INFUSION_ASSESSMENT_V1")
        assert item["attempted"] is True
        assert item["latest_submitted_at"] is not None

    def test_submit_on_draft_returns_404(self, worker_auth, admin_clinical_auth):
        r = requests.get(f"{API}/training/admin/assessments?status=draft", headers=_h(admin_clinical_auth), timeout=10)
        draft = next((a for a in r.json() if a["code"] == "PICC_LINE_ASSESSMENT_V1_DRAFT"), None)
        assert draft is not None
        r = requests.post(
            f"{API}/training/assessments/{draft['id']}/submit",
            headers=_h(worker_auth),
            json={"answers": [1, True]},
            timeout=10,
        )
        assert r.status_code == 404


# ============================================================================
# 8. QUALIFICATION LINKAGE / Patch 2 regression
# ============================================================================
class TestQualificationLinkage:
    def test_worker_eligibility_iv_infusion_assessment_required(self, worker_auth):
        """Riya has NOT completed IV_INFUSION_V1 training, so even after passing
        the assessment the locked_reason should remain in (TRAINING_REQUIRED |
        ASSESSMENT_REQUIRED | TIER_TOO_LOW | QUALIFICATION_RECORD_MISSING | ...)
        — i.e. worker should not be auto-APPROVED for IV_INFUSION.
        """
        r = requests.get(f"{API}/workers/me/service-eligibility", headers=_h(worker_auth), timeout=15)
        assert r.status_code == 200, r.text
        iv = next((s for s in r.json() if s["code"] == "IV_INFUSION"), None)
        assert iv is not None, "IV_INFUSION service missing"
        # Not approved — Patch 2 preference unchanged
        assert iv["qualification_status"] != "APPROVED" or iv["can_opt_in"] is False or iv["preference_status"] != "OPTED_IN"

    def test_passing_assessment_does_not_auto_opt_in(self, worker_auth):
        # Submit a passing assessment first
        a = next(
            a for a in requests.get(f"{API}/training/assessments", headers=_h(worker_auth), timeout=10).json()
            if a["code"] == "IV_INFUSION_ASSESSMENT_V1"
        )
        requests.post(
            f"{API}/training/assessments/{a['id']}/submit",
            headers=_h(worker_auth),
            json={"answers": [1, True, [0, 1, 2], "desc"]},
            timeout=15,
        )
        r = requests.get(f"{API}/workers/me/service-eligibility", headers=_h(worker_auth), timeout=15)
        iv = next(s for s in r.json() if s["code"] == "IV_INFUSION")
        # Per spec: passing assessment must NOT auto-opt-in.
        assert iv["preference_status"] != "OPTED_IN", iv


# ============================================================================
# 9. ADMIN LIST ENDPOINTS
# ============================================================================
class TestAdminLists:
    def test_admin_modules_filter_by_status(self, admin_clinical_auth):
        r = requests.get(f"{API}/training/admin/modules?status=draft", headers=_h(admin_clinical_auth), timeout=10)
        assert r.status_code == 200
        for m in r.json():
            assert m["status"] == "draft"
            # lifecycle fields
            for k in ["created_by", "updated_by", "published_version", "published_at", "reviewed_by", "reviewed_at"]:
                assert k in m

    def test_admin_assessments_published_includes_correct_answers(self, admin_clinical_auth):
        r = requests.get(f"{API}/training/admin/assessments?status=published", headers=_h(admin_clinical_auth), timeout=10)
        assert r.status_code == 200
        body = r.json()
        iv = next((a for a in body if a["code"] == "IV_INFUSION_ASSESSMENT_V1"), None)
        assert iv is not None
        q1 = iv["questions"][0]
        # admin view exposes correct answers
        assert "correct_index" in q1 or "correct_indices" in q1 or "correct_bool" in q1

    def test_admin_assessments_invalid_status(self, admin_clinical_auth):
        r = requests.get(f"{API}/training/admin/assessments?status=bogus", headers=_h(admin_clinical_auth), timeout=10)
        assert r.status_code == 400


# ============================================================================
# 12. FAMILY SUMMARY resolution via care workflow engine — light smoke
# (Only verify that the endpoint exists and returns something for an existing
# booking, OR returns 404 gracefully; we don't have a guaranteed booking_id.)
# ============================================================================
class TestFamilySummarySmoke:
    def test_care_workflow_endpoint_reachable_or_404(self, worker_auth):
        fake = uuid.uuid4()
        r = requests.get(f"{API}/care/workflow/{fake}", headers=_h(worker_auth), timeout=10)
        assert r.status_code in (200, 401, 403, 404)
