# seed_demo.py
"""Seed realistic vehicle policies for demonstration and Product Hunt preview."""
from datetime import date, timedelta
from sqlmodel import Session, select
from database import engine, init_db
from models import VehicleRecord

def seed():
    init_db()
    with Session(engine) as session:
        # Check if already seeded
        count = len(session.exec(select(VehicleRecord)).all())
        if count >= 3:
            print(f"Database already has {count} records.")
            return

        today = date.today()
        demo_records = [
            VehicleRecord(
                register_number="UP 80 BX 4412",
                vehicle_model="Mahindra 575 DI",
                owner_name="Sukhwinder Singh",
                owner_phone="+91 9812345678",
                address="Village Bichpuri, Agra, Uttar Pradesh",
                policy_number="ICICI-2024-884102",
                validity_date=today + timedelta(days=4),  # Critical / Urgent (4 days left)
                insurance_company="ICICI Lombard",
                notes="Comprehensive commercial tractor policy. Hypothecated to SBI Bichpuri.",
            ),
            VehicleRecord(
                register_number="UP 85 AZ 9011",
                vehicle_model="Swaraj 744 FE",
                owner_name="Birendra Yadav",
                owner_phone="+91 9876543210",
                address="Tehsil Mathura, Uttar Pradesh",
                policy_number="BAJ-77112044",
                validity_date=today + timedelta(days=18),  # Upcoming / Soon (18 days left)
                insurance_company="Bajaj Allianz",
                notes="Nil Depreciation cover. Harvest season active.",
            ),
            VehicleRecord(
                register_number="UP 80 CE 1205",
                vehicle_model="John Deere 5310",
                owner_name="Maheshwar Sharma",
                owner_phone="+91 9412356789",
                address="Fatehabad Road, Agra, Uttar Pradesh",
                policy_number="HDFC-ERGO-991201",
                validity_date=today + timedelta(days=145),  # Protected / Active (145 days left)
                insurance_company="HDFC Ergo",
                notes="Standard 1-year fleet cover.",
            ),
            VehicleRecord(
                register_number="UP 81 DF 3399",
                vehicle_model="Sonalika DI 745",
                owner_name="Dharmendra Choudhary",
                owner_phone="+91 9319876543",
                address="Sasni Gate, Aligarh, Uttar Pradesh",
                policy_number="POL-33019488",
                validity_date=today - timedelta(days=5),  # Expired (5 days ago)
                insurance_company="Reliance General",
                notes="Expired policy. Owner contacted on WhatsApp for urgent renewal.",
            ),
            VehicleRecord(
                register_number="UP 78 GH 7721",
                vehicle_model="Massey Ferguson 241 DI",
                owner_name="Rajesh Verma",
                owner_phone="+91 9555123456",
                address="GT Road, Kanpur Rural, Uttar Pradesh",
                policy_number="NIA-99011234",
                validity_date=today + timedelta(days=2),  # Critical / Urgent (2 days left)
                insurance_company="New India Assurance",
                notes="Third party + Own damage cover.",
            )
        ]

        for r in demo_records:
            session.add(r)
        session.commit()
        print(f"Successfully seeded {len(demo_records)} realistic fleet records!")

if __name__ == "__main__":
    seed()
