"""
One-off script to retry-assign every UNASSIGNED review ticket now that a
reviewer exists. Run this once after seed_reviewer.py.

USAGE (from the backend/ folder):
    python retry_assign_tickets.py
"""
import requests

BASE_URL = "http://localhost:8000/api"
ADMIN_EMAIL = "admin@nurseconnect.in"
ADMIN_PASSWORD = "Admin@1234"


def main():
    s = requests.Session()

    print("Logging in as admin...")
    r = s.post(f"{BASE_URL}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    r.raise_for_status()
    body = r.json()
    token = body.get("access_token") or body.get("tokens", {}).get("access_token")
    headers = {"Authorization": f"Bearer {token}"}
    print("  logged in.")

    print("Fetching unassigned tickets...")
    r = s.get(f"{BASE_URL}/admin/review/unassigned", headers=headers)
    r.raise_for_status()
    tickets = r.json()
    if not tickets:
        print("No unassigned tickets found.")
        return

    for t in tickets:
        tid = t["id"]
        r = s.post(f"{BASE_URL}/admin/review/tickets/{tid}/retry-assign", headers=headers)
        if r.status_code == 200:
            result = r.json()
            print(f"  ticket {tid} -> assigned_to: {result.get('assigned_to')}")
        else:
            print(f"  FAILED for ticket {tid}: {r.status_code} {r.text}")


if __name__ == "__main__":
    main()