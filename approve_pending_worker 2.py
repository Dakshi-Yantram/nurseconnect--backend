"""
One-off script to fully approve a pending worker via the real API endpoints
(not the DB) — logs in as admin, verifies all required documents, passes the
background check, then approves the worker.

USAGE:
    pip install requests   # if not already installed
    python approve_pending_worker.py

Adjust BASE_URL if your backend runs on a different host/port.
"""
import requests

BASE_URL = "http://localhost:8000/api"
ADMIN_EMAIL = "admin@nurseconnect.in"
ADMIN_PASSWORD = "Admin@1234"


def main():
    s = requests.Session()

    # 1. Login as admin
    print("Logging in as admin...")
    r = s.post(f"{BASE_URL}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    r.raise_for_status()
    body = r.json()
    token = (
        body.get("access_token")
        or body.get("token")
        or body.get("tokens", {}).get("access_token")
    )
    if not token:
        print("Could not find access token in login response:", body)
        return
    headers = {"Authorization": f"Bearer {token}"}
    print("  logged in.")

    # 2. Get pending workers
    print("Fetching pending workers...")
    r = s.get(f"{BASE_URL}/admin/workers/pending", headers=headers)
    r.raise_for_status()
    pending = r.json()
    if not pending:
        print("No pending workers found. Nothing to do.")
        return

    for worker in pending:
        worker_id = worker["worker_id"]
        print(f"\n--- Approving {worker['full_name']} ({worker_id}) ---")

        # 3. Verify every uploaded document
        for doc in worker["documents"]:
            doc_id = doc["id"]
            doc_type = doc["document_type"]
            r = s.patch(
                f"{BASE_URL}/admin/workers/{worker_id}/documents/{doc_id}",
                json={"status": "verified"},
                headers=headers,
            )
            if r.status_code == 200:
                print(f"  verified: {doc_type}")
            else:
                print(f"  FAILED to verify {doc_type}: {r.status_code} {r.text}")

        # 4. Pass background check
        r = s.post(
            f"{BASE_URL}/admin/workers/{worker_id}/background-check",
            json={"status": "passed"},
            headers=headers,
        )
        if r.status_code == 200:
            print("  background check: passed")
        else:
            print(f"  FAILED background check: {r.status_code} {r.text}")

        # 5. Approve
        r = s.post(f"{BASE_URL}/admin/workers/{worker_id}/approve", headers=headers)
        if r.status_code == 200:
            print(f"  APPROVED: {worker['full_name']}")
        else:
            print(f"  FAILED to approve: {r.status_code} {r.text}")


if __name__ == "__main__":
    main()