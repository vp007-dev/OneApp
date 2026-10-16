# models.py
from typing import Optional
from datetime import date, datetime, timezone
from sqlmodel import SQLModel, Field


def get_utc_now() -> datetime:
    return datetime.now(timezone.utc)


class VehicleRecord(SQLModel, table=True):
    __tablename__ = "vehiclerecord"

    id: Optional[int] = Field(default=None, primary_key=True)
    register_number: str = Field(index=True)      # e.g. "UP 80 XX 1234"
    vehicle_model: str                            # e.g. "Mahindra 575 DI"
    owner_name: str = Field(index=True)
    owner_phone: str = Field(default="")          # For WhatsApp / SMS reminders
    address: str = Field(default="")
    policy_number: str = Field(index=True)
    validity_date: date = Field(index=True)       # YYYY-MM-DD
    insurance_company: str = Field(default="")    # Insurance company name
    device_token: str = Field(default="")
    notes: str = Field(default="")
    last_alert_sent: Optional[date] = Field(default=None)
    created_at: datetime = Field(default_factory=get_utc_now)


class Device(SQLModel, table=True):
    __tablename__ = "device"

    id: Optional[int] = Field(default=None, primary_key=True)
    token: str = Field(unique=True, index=True)
    created_at: datetime = Field(default_factory=get_utc_now)