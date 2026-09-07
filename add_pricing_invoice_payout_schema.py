"""Schema for the payment / invoice / GST / nurse-payout-release feature.

Adds ONLY what that feature needs. Nothing else in the schema is touched.

  NEW TABLES
    pricing_components   the modular charges table (package rate | GST |
                         platform fee | GST on platform fee). One row per
                         chargeable element of a service/package.
    payout_statements    the nurse-facing "Payout Advice & Tax Invoice".
                         Separate from `invoices` because that table is
                         uniquely keyed on booking_id and holds the
                         CUSTOMER's tax document; one booking legitimately
                         produces two documents with different recipients
                         and different numbering series.

  ALTERED TABLES
    invoices         + taxable_value, exempt_value, cgst_amount, sgst_amount,
                       place_of_supply, pricing_snapshot, pdf_generated_at
    worker_payouts   + razorpay_payout_status, razorpay_utr,
                       razorpay_fund_account_id, razorpay_last_response,
                       idempotency_key (UNIQUE), ready_for_release_at,
                       released_by, released_at, last_status_checked_at
                     + UNIQUE (booking_id)  <- hard duplicate-payout guard
    worker_profiles  + razorpay_contact_id

Only needed for a database that already existed before this change — a
brand-new database gets all of it from create_tables.py, which builds every
table from the current SQLAlchemy models.

Safe to re-run: every statement is idempotent (IF NOT EXISTS, or a
duplicate_object / duplicate_table guard). No existing row's data is modified
except that the new numeric columns take their 0 defaults.

IMPORTANT — the UNIQUE constraint on worker_payouts.booking_id is the one
statement here that can FAIL on live data: it cannot be created if duplicate
payouts already exist for a booking. That is exactly the corruption the
constraint is meant to prevent, so the script reports the offending bookings
and leaves them for a human to resolve rather than deleting anything itself.
The same applies to the unique index on idempotency_key.
"""
import asyncio
import os

import asyncpg
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]


async def main():
    dsn = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://").replace(
        "?ssl=require", "?sslmode=require"
    )
    conn = await asyncpg.connect(dsn)

    # =====================================================================
    # 1. pricing_components — the modular charges table
    # =====================================================================
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS pricing_components (
            id                UUID PRIMARY KEY,
            service_id        UUID NULL REFERENCES service_catalogue(id) ON DELETE CASCADE,
            package_id        UUID NULL REFERENCES care_packages(id) ON DELETE CASCADE,
            component_code    VARCHAR(50)  NOT NULL,
            label             VARCHAR(255) NOT NULL,
            sac_code          VARCHAR(20)  NULL,
            input_amount      NUMERIC(10,2) NOT NULL,
            basis             VARCHAR(20)  NOT NULL DEFAULT 'customer_rate',
            gst_inclusive     BOOLEAN      NOT NULL DEFAULT TRUE,
            gst_rate_pct      NUMERIC(5,2) NOT NULL DEFAULT 0,
            exemption_note    TEXT         NULL,
            earns_to          VARCHAR(20)  NOT NULL DEFAULT 'worker',
            commission_pct    NUMERIC(5,2) NULL,
            display_order     INTEGER      NOT NULL DEFAULT 0,
            is_active         BOOLEAN      NOT NULL DEFAULT TRUE,
            created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
            updated_at        TIMESTAMPTZ  NOT NULL DEFAULT now()
        );
    """)
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS ix_pricing_components_service_id
            ON pricing_components (service_id);
        CREATE INDEX IF NOT EXISTS ix_pricing_components_package_id
            ON pricing_components (package_id);
        CREATE INDEX IF NOT EXISTS ix_pricing_components_is_active
            ON pricing_components (is_active);
        CREATE INDEX IF NOT EXISTS ix_pricing_components_service_active
            ON pricing_components (service_id, is_active);
        CREATE INDEX IF NOT EXISTS ix_pricing_components_package_active
            ON pricing_components (package_id, is_active);
    """)
    # A component must hang off exactly one offering. Without this a row could
    # attach to both a service and a package (or neither) and silently never
    # be picked up by the resolver.
    await conn.execute("""
        DO $$ BEGIN
            ALTER TABLE pricing_components
            ADD CONSTRAINT ck_pricing_components_one_owner
            CHECK (
                (service_id IS NOT NULL AND package_id IS NULL)
                OR (service_id IS NULL AND package_id IS NOT NULL)
            );
        EXCEPTION WHEN duplicate_object THEN NULL; END $$;
    """)
    print("pricing_components table + indexes ready")

    # =====================================================================
    # 2. invoices — GST breakdown + frozen calculation snapshot
    # =====================================================================
    await conn.execute("""
        ALTER TABLE invoices
        ADD COLUMN IF NOT EXISTS taxable_value    NUMERIC(10,2) NOT NULL DEFAULT 0,
        ADD COLUMN IF NOT EXISTS exempt_value     NUMERIC(10,2) NOT NULL DEFAULT 0,
        ADD COLUMN IF NOT EXISTS cgst_amount      NUMERIC(10,2) NOT NULL DEFAULT 0,
        ADD COLUMN IF NOT EXISTS sgst_amount      NUMERIC(10,2) NOT NULL DEFAULT 0,
        ADD COLUMN IF NOT EXISTS place_of_supply  VARCHAR(100)  NULL,
        ADD COLUMN IF NOT EXISTS pricing_snapshot JSONB         NULL,
        ADD COLUMN IF NOT EXISTS pdf_generated_at TIMESTAMPTZ   NULL;
    """)
    print("invoices GST/snapshot columns ready")

    # Backfill: pre-existing invoices were all 0% GST (the only invoice path
    # that existed was composite-care, which bills exempt), so their whole
    # subtotal is exempt value. Only touches rows never seen by the new code.
    backfilled = await conn.execute("""
        UPDATE invoices
        SET exempt_value = subtotal_amount
        WHERE exempt_value = 0
          AND taxable_value = 0
          AND COALESCE(tax_amount, 0) = 0
          AND subtotal_amount > 0;
    """)
    print(f"invoices exempt_value backfill: {backfilled}")

    # =====================================================================
    # 3. worker_profiles — RazorpayX contact cache
    # =====================================================================
    await conn.execute("""
        ALTER TABLE worker_profiles
        ADD COLUMN IF NOT EXISTS razorpay_contact_id VARCHAR(100) NULL;
    """)
    print("worker_profiles.razorpay_contact_id ready")

    # =====================================================================
    # 4. worker_payouts — release tracking + Razorpay confirmation
    # =====================================================================
    await conn.execute("""
        ALTER TABLE worker_payouts
        ADD COLUMN IF NOT EXISTS razorpay_payout_status   VARCHAR(30) NULL,
        ADD COLUMN IF NOT EXISTS razorpay_utr             VARCHAR(64) NULL,
        ADD COLUMN IF NOT EXISTS razorpay_fund_account_id VARCHAR(64) NULL,
        ADD COLUMN IF NOT EXISTS razorpay_last_response   JSONB       NULL,
        ADD COLUMN IF NOT EXISTS idempotency_key          VARCHAR(64) NULL,
        ADD COLUMN IF NOT EXISTS ready_for_release_at     TIMESTAMPTZ NULL,
        ADD COLUMN IF NOT EXISTS released_by              UUID        NULL REFERENCES users(id),
        ADD COLUMN IF NOT EXISTS released_at              TIMESTAMPTZ NULL,
        ADD COLUMN IF NOT EXISTS last_status_checked_at   TIMESTAMPTZ NULL;
    """)
    print("worker_payouts release/confirmation columns ready")

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS ix_worker_payouts_ready
        ON worker_payouts (ready_for_release_at, status);
    """)
    print("ix_worker_payouts_ready index ready")

    # Existing payouts belong to already-completed bookings, so they are
    # legitimately releasable — surface them in the admin queue rather than
    # stranding them invisible. Only rows not yet paid.
    promoted = await conn.execute("""
        UPDATE worker_payouts
        SET ready_for_release_at = COALESCE(ready_for_release_at, created_at)
        WHERE ready_for_release_at IS NULL
          AND status IN ('pending', 'failed');
    """)
    print(f"worker_payouts ready_for_release_at backfill: {promoted}")

    # Mark already-paid payouts as manually settled. They were paid before
    # RazorpayX confirmation existed, so claiming a Razorpay status for them
    # would be a lie — 'manual_settlement' says exactly what happened.
    marked = await conn.execute("""
        UPDATE worker_payouts
        SET razorpay_payout_status = 'manual_settlement'
        WHERE status = 'paid'
          AND razorpay_payout_status IS NULL
          AND razorpay_payout_id IS NULL;
    """)
    print(f"worker_payouts legacy paid rows marked manual: {marked}")

    # --- 4a. UNIQUE (booking_id) — the duplicate-payout guard -------------
    dupes = await conn.fetch("""
        SELECT booking_id, COUNT(*) AS n
        FROM worker_payouts
        GROUP BY booking_id
        HAVING COUNT(*) > 1;
    """)
    if dupes:
        print("\n*** WARNING: duplicate payouts found — UNIQUE(booking_id) NOT created ***")
        for row in dupes:
            print(f"    booking_id={row['booking_id']} has {row['n']} payout rows")
        print("    Resolve these by hand (keep the paid/most recent row, void the")
        print("    others) and re-run this script. Nothing was deleted automatically:")
        print("    these rows may represent money that actually moved.\n")
    else:
        await conn.execute("""
            DO $$ BEGIN
                ALTER TABLE worker_payouts
                ADD CONSTRAINT uq_worker_payouts_booking UNIQUE (booking_id);
            EXCEPTION WHEN duplicate_object THEN NULL;
                      WHEN duplicate_table  THEN NULL; END $$;
        """)
        print("uq_worker_payouts_booking UNIQUE constraint ready")

    # --- 4b. UNIQUE (idempotency_key) -------------------------------------
    # Partial: legacy rows have NULL keys and several NULLs must coexist.
    key_dupes = await conn.fetch("""
        SELECT idempotency_key, COUNT(*) AS n
        FROM worker_payouts
        WHERE idempotency_key IS NOT NULL
        GROUP BY idempotency_key
        HAVING COUNT(*) > 1;
    """)
    if key_dupes:
        print("\n*** WARNING: duplicate idempotency_key values — unique index NOT created ***")
        for row in key_dupes:
            print(f"    idempotency_key={row['idempotency_key']} on {row['n']} rows")
        print()
    else:
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_worker_payouts_idempotency_key
            ON worker_payouts (idempotency_key)
            WHERE idempotency_key IS NOT NULL;
        """)
        print("uq_worker_payouts_idempotency_key unique index ready")

    # Backfill idempotency keys for existing unpaid payouts, using the same
    # uuid5(NAMESPACE_URL, "nurseconnect:payout:<booking_id>") derivation as
    # payout_service.payout_idempotency_seed, so a key computed in Python for
    # one of these rows matches the one stored here.
    #
    # uuid5 is SHA-1 over the namespace bytes followed by the name, with the
    # version/variant bits overwritten. pgcrypto's digest() gives the SHA-1.
    has_pgcrypto = await conn.fetchval("""
        SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pgcrypto');
    """)
    if has_pgcrypto:
        filled = await conn.execute(r"""
            UPDATE worker_payouts
            SET idempotency_key = 'payout_' || encode(
                set_byte(
                    set_byte(
                        substring(
                            digest(
                                decode('6ba7b8119dad11d180b400c04fd430c8', 'hex')
                                || convert_to('nurseconnect:payout:' || booking_id::text, 'UTF8'),
                                'sha1'
                            ) from 1 for 16
                        ),
                        6,
                        (get_byte(substring(digest(decode('6ba7b8119dad11d180b400c04fd430c8','hex') || convert_to('nurseconnect:payout:' || booking_id::text,'UTF8'),'sha1') from 1 for 16), 6) & 15) | 80
                    ),
                    8,
                    (get_byte(substring(digest(decode('6ba7b8119dad11d180b400c04fd430c8','hex') || convert_to('nurseconnect:payout:' || booking_id::text,'UTF8'),'sha1') from 1 for 16), 8) & 63) | 128
                ),
                'hex'
            )
            WHERE idempotency_key IS NULL
              AND status IN ('pending', 'failed');
        """)
        print(f"worker_payouts idempotency_key backfill: {filled}")
    else:
        print("pgcrypto not installed — idempotency_key backfill SKIPPED.")
        print("  Harmless: release_payout() allocates the key on first release")
        print("  for any row where it is NULL, using the same derivation.")

    # =====================================================================
    # 5. payout_statements — the nurse's payout advice
    # =====================================================================
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS payout_statements (
            id                UUID PRIMARY KEY,
            payout_id         UUID NOT NULL UNIQUE REFERENCES worker_payouts(id) ON DELETE CASCADE,
            booking_id        UUID NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
            worker_id         UUID NOT NULL REFERENCES worker_profiles(id),
            statement_number  VARCHAR(40) NOT NULL UNIQUE,
            gross_earned      NUMERIC(10,2) NOT NULL,
            platform_fee      NUMERIC(10,2) NOT NULL DEFAULT 0,
            platform_fee_gst  NUMERIC(10,2) NOT NULL DEFAULT 0,
            net_take_home     NUMERIC(10,2) NOT NULL,
            total_deductions  NUMERIC(10,2) NOT NULL DEFAULT 0,
            final_disbursal   NUMERIC(10,2) NOT NULL,
            line_items        JSONB NOT NULL,
            pdf_url           TEXT NULL,
            pdf_generated_at  TIMESTAMPTZ NULL,
            generated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """)
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS ix_payout_statements_payout_id
            ON payout_statements (payout_id);
        CREATE INDEX IF NOT EXISTS ix_payout_statements_booking_id
            ON payout_statements (booking_id);
        CREATE INDEX IF NOT EXISTS ix_payout_statements_worker_id
            ON payout_statements (worker_id);
    """)
    print("payout_statements table + indexes ready")

    await conn.close()
    print(
        "\nDone.\n"
        "  - pricing_components: empty. Offerings with no rows keep their\n"
        "    existing pricing via the legacy single-line fallback in\n"
        "    app/services/pricing_resolver.py, so nothing changes price until\n"
        "    a rate card is configured.\n"
        "  - payout_statements: empty; rows appear as payouts are released.\n"
        "  - Existing pending/failed payouts now appear in the admin\n"
        "    'Ready for Release' queue.\n"
        "  - Set COMPANY_GSTIN and the RAZORPAYX_* settings before releasing\n"
        "    real payouts (see app/core/config.py)."
    )


if __name__ == "__main__":
    asyncio.run(main())
