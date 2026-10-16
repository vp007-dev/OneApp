# migrate_db.py
"""
Utility script to migrate data from local SQLite (tractorcare.db) to a free Cloud PostgreSQL
database (Neon.tech, Supabase, Render Postgres, etc.) without losing any records.

Usage:
    python migrate_db.py
    # OR provide target URL explicitly:
    python migrate_db.py "postgresql://user:password@ep-xyz.neon.tech/neondb?sslmode=require"
"""

import sys
import os
from sqlmodel import SQLModel, Session, select, create_engine
from models import VehicleRecord, Device
from database import DB_PATH, init_db

def run_migration(target_url: str):
    print("=" * 60)
    print("🚜 TRACTORCARE DATA MIGRATION: SQLite -> Cloud PostgreSQL")
    print("=" * 60)

    # Clean target URL format if needed
    if target_url.startswith("postgres://"):
        target_url = target_url.replace("postgres://", "postgresql://", 1)

    sqlite_url = f"sqlite:///{DB_PATH}"
    print(f"📂 Source: {sqlite_url}")
    print(f"☁️ Target: {target_url.split('@')[-1] if '@' in target_url else target_url}")

    # Connect to source SQLite
    source_engine = create_engine(sqlite_url, connect_args={"check_same_thread": False})
    
    # Connect to target Postgres
    target_engine = create_engine(target_url, pool_pre_ping=True)

    # Initialize tables on target
    print("\n1. Ensuring tables exist in target database...")
    SQLModel.metadata.create_all(target_engine)
    print("   ✅ Tables ready.")

    # Read records from SQLite
    with Session(source_engine) as src_session, Session(target_engine) as tgt_session:
        print("\n2. Fetching records from local SQLite...")
        records = src_session.exec(select(VehicleRecord)).all()
        devices = src_session.exec(select(Device)).all()
        print(f"   Found {len(records)} vehicle records and {len(devices)} device tokens.")

        # Migrate Vehicle Records
        print("\n3. Migrating vehicle records to PostgreSQL...")
        migrated_records = 0
        skipped_records = 0
        for r in records:
            # Check if record already exists by register_number + policy_number
            existing = tgt_session.exec(
                select(VehicleRecord).where(
                    VehicleRecord.register_number == r.register_number,
                    VehicleRecord.policy_number == r.policy_number,
                )
            ).first()

            if not existing:
                new_rec = VehicleRecord(
                    register_number=r.register_number,
                    vehicle_model=r.vehicle_model,
                    owner_name=r.owner_name,
                    owner_phone=r.owner_phone or "",
                    address=r.address or "",
                    policy_number=r.policy_number,
                    validity_date=r.validity_date,
                    insurance_company=r.insurance_company or "",
                    device_token=r.device_token or "",
                    notes=r.notes or "",
                    last_alert_sent=r.last_alert_sent,
                    created_at=r.created_at,
                )
                tgt_session.add(new_rec)
                migrated_records += 1
            else:
                skipped_records += 1

        # Migrate Devices
        print("4. Migrating registered devices...")
        migrated_devices = 0
        for d in devices:
            existing_dev = tgt_session.exec(select(Device).where(Device.token == d.token)).first()
            if not existing_dev:
                tgt_session.add(Device(token=d.token, created_at=d.created_at))
                migrated_devices += 1

        tgt_session.commit()

    print("\n" + "=" * 60)
    print("🎉 MIGRATION COMPLETED SUCCESSFULLY!")
    print(f"   - Vehicle Records Migrated: {migrated_records} (Skipped duplicate: {skipped_records})")
    print(f"   - Device Tokens Migrated:  {migrated_devices}")
    print("=" * 60)


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else os.getenv("TARGET_DATABASE_URL", "")
    if not url:
        print("❌ Error: Target database URL not provided.")
        print("\nUsage:")
        print("    python migrate_db.py \"postgresql://user:password@ep-xyz.neon.tech/neondb?sslmode=require\"")
        print("Or set TARGET_DATABASE_URL environment variable.")
        sys.exit(1)

    run_migration(url)
