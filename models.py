# models.py
from typing import Optional
from datetime import date
from sqlmodel import SQLModel, Field


class VehicleRecord(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)

    register_number: str       # e.g. "UP 80 XX 1234"
    vehicle_model: str         # e.g. "Mahindra 575 DI"
    owner_name: str
    address: str
    policy_number: str
    validity_date: date        # YYYY-MM-DD
    insurance_company: str     # NEW: Insurance company name
    device_token: str = Field(default="")

class Device(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    token: str