"""Delete visit care-summary PDFs the old flow uploaded to PUBLIC Cloudinary URLs.

Before this change, every "Download report" click uploaded the care summary
(vitals, patient name; nurse copies also clinical notes) to Cloudinary's
`visit-reports/` folder with a public, non-expiring URL. The new code no
longer uploads anything, but the files already there stay reachable by
anyone who has a link until they are deleted.

Usage (from the backend root, with the production env loaded):
    python -m scripts.purge_public_visit_reports            # dry run: list only
    python -m scripts.purge_public_visit_reports --delete   # actually delete

Cloudinary CDN caches can keep serving a deleted asset for a while; the
script passes invalidate=True so the CDN copy is purged as well.
"""
from __future__ import annotations

import argparse
import sys

PREFIX = "visit-reports/"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--delete", action="store_true", help="delete (default is a dry run)")
    args = ap.parse_args()

    from app.core.config import settings
    import cloudinary
    import cloudinary.api

    if not (settings.CLOUDINARY_CLOUD_NAME and settings.CLOUDINARY_API_KEY and settings.CLOUDINARY_API_SECRET):
        print("Cloudinary credentials are not configured; nothing to do.")
        return 1
    cloudinary.config(
        cloud_name=settings.CLOUDINARY_CLOUD_NAME,
        api_key=settings.CLOUDINARY_API_KEY,
        api_secret=settings.CLOUDINARY_API_SECRET,
    )

    total = 0
    # resource_type="auto" stores PDFs as "image" (Cloudinary treats PDF as a
    # multi-page image) and occasionally as "raw" — check both.
    for rtype in ("image", "raw"):
        cursor = None
        while True:
            kw = dict(type="upload", prefix=PREFIX, max_results=500, resource_type=rtype)
            if cursor:
                kw["next_cursor"] = cursor
            page = cloudinary.api.resources(**kw)
            ids = [r["public_id"] for r in page.get("resources", [])]
            total += len(ids)
            for pid in ids:
                print(f"[{rtype}] {pid}")
            if args.delete and ids:
                cloudinary.api.delete_resources(ids, resource_type=rtype, invalidate=True)
            cursor = page.get("next_cursor")
            if not cursor:
                break

    verb = "Deleted" if args.delete else "Found (dry run)"
    print(f"{verb}: {total} public visit-report file(s) under '{PREFIX}'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
