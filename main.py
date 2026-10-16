# main.py
import os
import io
import csv
import json
import re
import time
import secrets
import smtplib
from datetime import date, datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Dict, Any, List, Optional
from urllib.parse import quote

from dotenv import load_dotenv
load_dotenv()

from fastapi import (
    FastAPI,
    Request,
    Depends,
    Form,
    UploadFile,
    File,
    HTTPException,
    status,
)
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    JSONResponse,
    FileResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from sqlmodel import Session, select, func
from apscheduler.schedulers.background import BackgroundScheduler
from starlette.middleware.sessions import SessionMiddleware

import pytesseract
from PIL import Image
import openpyxl
from openpyxl.utils.datetime import from_excel as openpyxl_from_excel
import requests

from models import VehicleRecord, Device
from database import engine, init_db, get_session, IS_SQLITE
from security import (
    APP_USERNAME,
    APP_PASSWORD,
    IS_DEFAULT_CREDENTIALS,
    SESSION_SECRET_KEY,
    verify_credentials,
    check_login_rate_limit,
    record_failed_login,
    clear_failed_logins,
    get_csrf_token,
    verify_csrf,
    SecurityHeadersMiddleware,
    validate_uploaded_file,
    MAX_FILE_SIZE,
)

# Protect PIL against decompression bomb attacks
Image.MAX_IMAGE_PIXELS = 10_000_000

# Session configuration
SESSION_TIMEOUT_SECONDS = int(os.getenv("SESSION_TIMEOUT_SECONDS", "3600"))  # 1 hour default
ALERT_EMAIL = os.getenv("ALERT_EMAIL", "").strip()
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_EMAIL = os.getenv("SMTP_EMAIL", "").strip()
SMTP_PASS = os.getenv("SMTP_PASS", "").strip()
MAILGUN_KEY = os.getenv("MAILGUN_KEY", "").strip()
MAILGUN_DOMAIN = os.getenv("MAILGUN_DOMAIN", "").strip()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()

# ---------- Firebase Admin Initialization (Safe & Graceful) ----------
FIREBASE_ENABLED = False
FIREBASE_PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "tractorcare-8586f").strip()
FIREBASE_CLIENT_EMAIL = os.getenv("FIREBASE_CLIENT_EMAIL", "").strip()
raw_firebase_key = os.getenv("FIREBASE_PRIVATE_KEY", "").strip()

if raw_firebase_key and FIREBASE_CLIENT_EMAIL:
    try:
        import firebase_admin
        from firebase_admin import credentials, messaging
        formatted_key = raw_firebase_key.replace("\\n", "\n")
        cred = credentials.Certificate({
            "type": "service_account",
            "project_id": FIREBASE_PROJECT_ID,
            "private_key": formatted_key,
            "client_email": FIREBASE_CLIENT_EMAIL,
            "token_uri": "https://oauth2.googleapis.com/token",
        })
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)
        FIREBASE_ENABLED = True
        print("✅ Firebase Admin SDK initialized successfully.")
    except Exception as e:
        print(f"⚠️ Firebase initialization skipped or failed: {e}")
else:
    print("ℹ️ Firebase private key not configured. Push notifications operating in standby mode.")

# ---------- FastAPI Application Setup ----------
app = FastAPI(
    title="TractorCare - Vehicle & Fleet Insurance Intelligence",
    description="Production-grade vehicle insurance and policy renewal tracking platform.",
    version="2.0.0",
    docs_url=None,  # Disabled in production for security; enabled via /docs if needed
    redoc_url=None,
)

# Apply OWASP Security Headers Middleware
app.add_middleware(SecurityHeadersMiddleware)

# Add secure signed session middleware
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET_KEY,
    session_cookie="tractorcare_session",
    max_age=86400 * 7,  # 7 days
    same_site="lax",
    https_only=os.getenv("ENVIRONMENT", "").lower() == "production",
)

# Mount static files and templates
os.makedirs("static", exist_ok=True)
os.makedirs("templates", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Background scheduler
scheduler = BackgroundScheduler(daemon=True)


# ---------- Notification Helpers ----------
def send_email(subject: str, body: str, recipient: Optional[str] = None) -> bool:
    """Send alert email via Mailgun or standard SMTP with error handling."""
    to_email = recipient or ALERT_EMAIL or SMTP_EMAIL
    if not to_email:
        print("ℹ️ Email alert skipped: No recipient or ALERT_EMAIL configured.")
        return False

    # 1. Try Mailgun API if configured
    if MAILGUN_KEY and MAILGUN_DOMAIN:
        try:
            resp = requests.post(
                f"https://api.mailgun.net/v3/{MAILGUN_DOMAIN}/messages",
                auth=("api", MAILGUN_KEY),
                data={
                    "from": f"TractorCare Alerts <mailgun@{MAILGUN_DOMAIN}>",
                    "to": [to_email],
                    "subject": subject,
                    "text": body,
                },
                timeout=15,
            )
            if resp.status_code < 300:
                print(f"✅ Email delivered via Mailgun to {to_email}")
                return True
            else:
                print(f"⚠️ Mailgun failed ({resp.status_code}): {resp.text[:120]}")
        except Exception as e:
            print(f"⚠️ Mailgun network error: {e}")

    # 2. Try SMTP fallback if configured
    if SMTP_EMAIL and SMTP_PASS:
        try:
            msg = EmailMessage()
            msg["Subject"] = subject
            msg["From"] = f"TractorCare <{SMTP_EMAIL}>"
            msg["To"] = to_email
            msg.set_content(body)

            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
                server.starttls()
                server.login(SMTP_EMAIL, SMTP_PASS)
                server.send_message(msg)
            print(f"✅ Email delivered via SMTP to {to_email}")
            return True
        except Exception as e:
            print(f"⚠️ SMTP delivery error: {e}")

    return False


def send_push(token: str, title: str, body: str) -> bool:
    """Send Firebase push notification if enabled."""
    if not FIREBASE_ENABLED or not token:
        return False
    try:
        from firebase_admin import messaging
        msg = messaging.Message(
            token=token,
            notification=messaging.Notification(title=title, body=body),
        )
        messaging.send(msg)
        return True
    except Exception as e:
        print(f"⚠️ Push notification error: {e}")
        return False


# ---------- Scheduler: Idempotent Policy Checker ----------
def check_expiring_policies():
    """Background check for upcoming or expired policies. Idempotent per day."""
    today = date.today()
    try:
        with Session(engine) as session:
            records = session.exec(select(VehicleRecord)).all()
            for r in records:
                days_left = (r.validity_date - today).days

                # Only send if we haven't sent an alert for this record today
                if r.last_alert_sent == today:
                    continue

                should_alert = False
                subject = ""

                if days_left == 7:
                    should_alert = True
                    subject = f"⚠️ [Urgent] Policy Expiring in 7 Days: {r.vehicle_model} ({r.register_number})"
                elif days_left == 1:
                    should_alert = True
                    subject = f"🚨 [Critical] Policy Expires Tomorrow: {r.vehicle_model} ({r.register_number})"
                elif days_left == 0:
                    should_alert = True
                    subject = f"🚨 [Action Required] Policy Expires Today: {r.vehicle_model} ({r.register_number})"
                elif days_left == -1:
                    should_alert = True
                    subject = f"❌ [Expired] Policy Has Lapsed: {r.vehicle_model} ({r.register_number})"

                if should_alert:
                    body = f"""TRACTORCARE POLICY RENEWAL NOTIFICATION
--------------------------------------------------
Vehicle Model:   {r.vehicle_model}
Registration:    {r.register_number}
Owner Name:      {r.owner_name}
Owner Phone:     {r.owner_phone or 'N/A'}
Policy Number:   {r.policy_number}
Insurance Co:    {r.insurance_company or 'N/A'}
Validity Date:   {r.validity_date} ({days_left} days remaining)

Please contact the owner or insurance company immediately to process renewal.
Dashboard: http://localhost:8000/records/{r.id}
"""
                    email_ok = send_email(subject, body)
                    push_ok = False
                    if r.device_token:
                        push_ok = send_push(
                            r.device_token,
                            f"⚠️ {r.vehicle_model} Renewal Alert",
                            f"Policy {r.policy_number} expires on {r.validity_date} ({days_left}d left).",
                        )

                    if email_ok or push_ok:
                        r.last_alert_sent = today
                        session.add(r)

            session.commit()
    except Exception as e:
        print(f"Scheduler job error: {e}")


# ---------- Authentication & Security Guard ----------
def require_login(request: Request) -> str:
    """Dependency protecting private endpoints. Redirects unauthenticated visitors to /login."""
    sess = request.session
    if not sess.get("logged_in"):
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"},
        )

    now = time.time()
    last_seen = sess.get("last_seen", 0)

    # Session timeout check
    if SESSION_TIMEOUT_SECONDS and last_seen and (now - last_seen) > SESSION_TIMEOUT_SECONDS:
        sess.clear()
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login?expired=1"},
        )

    # Update activity timestamp
    sess["last_seen"] = now
    return sess.get("user", "admin")


# ---------- OCR & Smart Heuristic Extraction ----------
INDIAN_COMPANIES = [
    "ICICI Lombard", "Bajaj Allianz", "HDFC Ergo", "Reliance General",
    "Tata AIG", "New India Assurance", "Oriental Insurance",
    "National Insurance", "United India Insurance", "IFFCO Tokio",
    "SBI General", "Cholamandalam MS", "Universal Sompo", "Digit Insurance",
    "Go Digit", "Shriram General", "Magma HDI", "Royal Sundaram",
]

POPULAR_TRACTORS = [
    "Mahindra 575 DI", "Mahindra 475 DI", "Mahindra 275 DI", "Mahindra Yuvo",
    "Swaraj 744 FE", "Swaraj 855 FE", "Swaraj 735 FE",
    "John Deere 5310", "John Deere 5050 D", "John Deere 5105",
    "Sonalika DI 745", "Sonalika DI 35", "Sonalika Tiger",
    "Massey Ferguson 241 DI", "Massey Ferguson 1035 DI",
    "New Holland 3630 TX", "New Holland 3230", "Eicher 380", "Eicher 485",
    "Farmtrac 60", "Powertrac Euro 50", "Kubota MU4501",
]


def heuristic_insurance_guess(policy_number: str, raw_text: str = "") -> str:
    """Determine insurance company using policy prefixes and text keywords."""
    combined = f"{policy_number} {raw_text}".upper()
    if "ICICI" in combined or policy_number.upper().startswith("IC"):
        return "ICICI Lombard"
    if "BAJAJ" in combined or policy_number.upper().startswith("BAJ"):
        return "Bajaj Allianz"
    if "HDFC" in combined:
        return "HDFC Ergo"
    if "RELIANCE" in combined or policy_number.upper().startswith("RAJ"):
        return "Reliance General"
    if "TATA" in combined:
        return "Tata AIG"
    if "NEW INDIA" in combined:
        return "New India Assurance"
    if "ORIENTAL" in combined:
        return "Oriental Insurance"
    if "NATIONAL" in combined:
        return "National Insurance"
    if "UNITED INDIA" in combined:
        return "United India Insurance"
    if "IFFCO" in combined:
        return "IFFCO Tokio"
    if "SBI" in combined:
        return "SBI General"
    if "CHOLA" in combined:
        return "Cholamandalam MS"
    if "DIGIT" in combined:
        return "Digit Insurance"
    if "SHRIRAM" in combined:
        return "Shriram General"
    if policy_number.upper().startswith("POL"):
        return "General Insurance Co."
    return ""


def heuristic_extract_fields(text: str) -> Dict[str, str]:
    """Smart offline regex & heuristic extractor for vehicle insurance documents."""
    fields = {
        "register_number": "",
        "vehicle_model": "",
        "owner_name": "",
        "address": "",
        "policy_number": "",
        "validity_date": "",
        "insurance_company": "",
        "owner_phone": "",
    }

    # 1. Registration Number (e.g. UP 80 AB 1234, DL 01 A 1234, HR-26-BR-9044)
    reg_match = re.search(
        r"\b([A-Z]{2}[ -]?[0-9]{1,2}[ -]?[A-Z]{1,3}[ -]?[0-9]{3,4})\b",
        text,
        re.IGNORECASE,
    )
    if reg_match:
        raw_reg = reg_match.group(1).upper()
        # Clean into standard space-separated format
        fields["register_number"] = re.sub(r"[- ]+", " ", raw_reg).strip()

    # 2. Policy Number (e.g. Policy No: 12345/67/89/000001, POL-1234567, 3302/012345)
    policy_match = re.search(
        r"(?:policy|pol\.?|certificate)\s*(?:no\.?|number|#)?\s*[:\-]?\s*([A-Z0-9\/\-]{7,30})",
        text,
        re.IGNORECASE,
    )
    if policy_match:
        fields["policy_number"] = policy_match.group(1).strip()
    else:
        # Fallback standalone alphanumeric sequence
        poly_cand = re.search(r"\b([0-9]{8,18})\b", text)
        if poly_cand:
            fields["policy_number"] = poly_cand.group(1).strip()

    # 3. Validity Date (e.g. Valid Till, Expiry Date, Period of Insurance)
    date_candidates = re.findall(r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})", text)
    if date_candidates:
        # Prefer the last date mentioned or parse candidates
        parsed_dates = []
        for cand in date_candidates:
            d = try_parse_date_string(cand)
            if d:
                parsed_dates.append(d)
        if parsed_dates:
            # Pick future date or maximum date as validity expiration
            max_d = max(parsed_dates)
            fields["validity_date"] = max_d.isoformat()

    # 4. Insurance Company
    fields["insurance_company"] = heuristic_insurance_guess(fields["policy_number"], text)

    # 5. Vehicle Model heuristics
    for tractor in POPULAR_TRACTORS:
        brand = tractor.split()[0].lower()
        if brand in text.lower():
            # Try to match the exact model or brand
            fields["vehicle_model"] = tractor
            break

    # 6. Phone number (e.g. +91 9876543210 or 10-digit Indian mobile)
    phone_match = re.search(r"\b(?:\+91[ -]?)?([6-9]\d{9})\b", text)
    if phone_match:
        fields["owner_phone"] = "+91 " + phone_match.group(1)

    return fields


def try_parse_date_string(s: str) -> Optional[date]:
    """Parse multiple common date formats into a datetime.date."""
    if not s:
        return None
    s = s.strip()
    formats = (
        "%d.%m.%Y", "%d.%m.%y",
        "%d/%m/%Y", "%d/%m/%y",
        "%Y-%m-%d", "%d-%m-%Y",
        "%d-%m-%y", "%m/%d/%Y",
    )
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            pass
    try:
        return date.fromisoformat(s)
    except Exception:
        pass
    m = re.search(r"(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})", s)
    if m:
        day, mon, yr = m.group(1), m.group(2), m.group(3)
        if len(yr) == 2:
            yr = "20" + yr
        try:
            return date(int(yr), int(mon), int(day))
        except Exception:
            pass
    return None


def extract_end_date_from_cell(cell) -> str:
    """Robust Excel cell date parser extracting the end/expiration date."""
    if cell is None:
        return ""
    if isinstance(cell, datetime):
        return cell.date().isoformat()
    if isinstance(cell, date):
        return cell.isoformat()
    if isinstance(cell, (int, float)):
        try:
            dt = openpyxl_from_excel(cell)
            if isinstance(dt, datetime):
                return dt.date().isoformat()
            if isinstance(dt, date):
                return dt.isoformat()
        except Exception:
            pass
    s = str(cell).strip()
    if re.search(r"\bto\b", s, flags=re.IGNORECASE):
        parts = re.split(r"\bto\b", s, flags=re.IGNORECASE)
        if len(parts) >= 2:
            d = try_parse_date_string(parts[1].strip())
            if d:
                return d.isoformat()
    if "-" in s and len(s.split("-")) >= 2:
        parts = [p.strip() for p in s.split("-")]
        d = try_parse_date_string(parts[-1])
        if d:
            return d.isoformat()
    d = try_parse_date_string(s)
    if d:
        return d.isoformat()
    return ""


def extract_text_from_bytes(raw_bytes: bytes, filename: str) -> str:
    """Extract text safely from image, Excel, or plain text."""
    lower_fn = filename.lower()
    if lower_fn.endswith((".png", ".jpg", ".jpeg", ".webp")):
        try:
            img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
            return pytesseract.image_to_string(img)
        except Exception as e:
            print(f"OCR execution error: {e}")
            return ""

    if lower_fn.endswith((".xlsx", ".xlsm")):
        try:
            wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), data_only=True, read_only=True)
            text_lines = []
            for sheet in wb.sheetnames:
                ws = wb[sheet]
                for row in ws.iter_rows(values_only=True):
                    text_lines.append(" | ".join([str(c) for c in row if c is not None]))
            return "\n".join(text_lines)
        except Exception as e:
            print(f"Excel reading error: {e}")
            return ""

    try:
        return raw_bytes.decode("utf-8", errors="ignore")
    except Exception:
        return ""


def parse_excel_rows(raw_bytes: bytes) -> List[Dict[str, str]]:
    """Parse Excel workbook rows with intelligent header mapping."""
    try:
        wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), data_only=True)
        ws = wb[wb.sheetnames[0]]
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            return []

        headers = [str(h).strip().lower() if h is not None else "" for h in rows[0]]
        key_map = {}
        for i, h in enumerate(headers):
            if "model" in h or "tractor" in h or "vehicle" in h:
                key_map[i] = "vehicle_model"
            elif any(x in h for x in ("r.c", "rc", "rc no", "reg", "register")):
                key_map[i] = "register_number"
            elif "phone" in h or "mobile" in h or "contact" in h:
                key_map[i] = "owner_phone"
            elif "name" in h and "ins" not in h:
                key_map[i] = "owner_name"
            elif "address" in h:
                key_map[i] = "address"
            elif "valid" in h or "expiry" in h or "due" in h:
                key_map[i] = "validity_date"
            elif "policy" in h:
                key_map[i] = "policy_number"
            elif "insurance" in h or "company" in h:
                key_map[i] = "insurance_company"

        results = []
        for r in rows[1:]:
            # Skip empty rows
            if not any(r):
                continue
            entry = {
                "register_number": "",
                "vehicle_model": "",
                "owner_name": "",
                "owner_phone": "",
                "policy_number": "",
                "validity_date": "",
                "address": "",
                "insurance_company": "",
            }
            for i, cell in enumerate(r):
                if i not in key_map:
                    continue
                key = key_map[i]
                if key == "validity_date":
                    entry[key] = extract_end_date_from_cell(cell)
                else:
                    entry[key] = "" if cell is None else str(cell).strip()

            # Autoguess insurance if missing
            if not entry["insurance_company"] and entry["policy_number"]:
                entry["insurance_company"] = heuristic_insurance_guess(entry["policy_number"])

            results.append(entry)
        return results
    except Exception as e:
        print(f"Excel parsing exception: {e}")
        return []


def ai_extract_fields_openrouter(text: str) -> Dict[str, str]:
    """OpenRouter LLM extraction with graceful fallback to heuristic extractor."""
    if not OPENROUTER_API_KEY:
        return heuristic_extract_fields(text)

    prompt = f"""
Extract the vehicle insurance details from the text below into clean JSON.
Ensure dates are in YYYY-MM-DD format.

Required JSON format:
{{
  "register_number": "",
  "vehicle_model": "",
  "owner_name": "",
  "owner_phone": "",
  "address": "",
  "policy_number": "",
  "validity_date": "",
  "insurance_company": ""
}}

If any field is missing or uncertain, return an empty string "".

DOCUMENT TEXT:
{text[:4000]}
"""
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://tractorcare.app",
        "X-Title": "TractorCare Insurance",
    }
    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": "You are a precise JSON vehicle insurance data extractor. Respond with valid JSON only."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,
        "max_tokens": 500,
    }
    try:
        resp = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload, timeout=25)
        if resp.status_code == 200:
            content = resp.json()["choices"][0]["message"]["content"]
            match = re.search(r"(\{[\s\S]*?\})", content)
            if match:
                parsed = json.loads(match.group(1))
                # Validate date
                if parsed.get("validity_date"):
                    parsed_date = try_parse_date_string(parsed["validity_date"])
                    parsed["validity_date"] = parsed_date.isoformat() if parsed_date else ""
                return {k: str(parsed.get(k, "") or "").strip() for k in [
                    "register_number", "vehicle_model", "owner_name", "owner_phone",
                    "address", "policy_number", "validity_date", "insurance_company"
                ]}
    except Exception as e:
        print(f"OpenRouter extraction error, falling back to heuristics: {e}")

    return heuristic_extract_fields(text)


def ai_guess_insurance_for_row(row_text: str) -> str:
    """Guess insurance company for a single row using AI or heuristics."""
    heuristic = heuristic_insurance_guess("", row_text)
    if heuristic:
        return heuristic
    if not OPENROUTER_API_KEY:
        return ""

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": "Return only the insurance company name (e.g. ICICI Lombard, Bajaj Allianz, HDFC Ergo) or empty string."},
            {"role": "user", "content": f"Row text: {row_text}"},
        ],
        "temperature": 0.0,
        "max_tokens": 30,
    }
    try:
        resp = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload, timeout=12)
        if resp.status_code == 200:
            val = resp.json()["choices"][0]["message"]["content"].strip()
            if len(val) < 80 and "{" not in val:
                return val
    except Exception:
        pass
    return ""


# ---------- Startup / Shutdown Events ----------
@app.on_event("startup")
def on_startup():
    init_db()
    try:
        if not scheduler.running:
            scheduler.add_job(check_expiring_policies, "interval", hours=1, id="policy_checker", replace_existing=True)
            scheduler.start()
            print("🕒 Policy expiration scheduler started (hourly check).")
    except Exception as e:
        print(f"Scheduler startup note: {e}")


@app.on_event("shutdown")
def on_shutdown():
    try:
        if scheduler.running:
            scheduler.shutdown(wait=False)
    except Exception:
        pass


# ---------- Public Health & Ops Endpoints ----------
@app.get("/health")
def health_check(session: Session = Depends(get_session)):
    """Production health check probe for uptime monitors and platforms."""
    count = session.exec(select(func.count(VehicleRecord.id))).one()
    return {
        "status": "healthy",
        "app": "TractorCare",
        "version": "2.0.0",
        "database": "sqlite_wal" if IS_SQLITE else "postgresql",
        "total_records": count,
        "firebase_push": "active" if FIREBASE_ENABLED else "standby",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ---------- Authentication Routes ----------
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if request.session.get("logged_in"):
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)

    csrf_token = get_csrf_token(request)
    expired = request.query_params.get("expired") == "1"
    error = "Your session expired. Please sign in again." if expired else None

    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "error": error,
            "csrf_token": csrf_token,
            "is_default_creds": IS_DEFAULT_CREDENTIALS,
            "default_user": APP_USERNAME,
            "default_pass": APP_PASSWORD if IS_DEFAULT_CREDENTIALS else "",
        },
    )


@app.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(""),
):
    client_ip = request.client.host if request.client else "unknown"

    # Rate limiting check
    allowed, seconds_left = check_login_rate_limit(client_ip)
    if not allowed:
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "error": f"Too many failed attempts. Please wait {seconds_left} seconds before trying again.",
                "csrf_token": get_csrf_token(request),
                "is_default_creds": IS_DEFAULT_CREDENTIALS,
            },
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    # CSRF check
    if not verify_csrf(request, csrf_token):
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "error": "Security validation failed (invalid CSRF token). Please try again.",
                "csrf_token": get_csrf_token(request),
                "is_default_creds": IS_DEFAULT_CREDENTIALS,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if verify_credentials(username, password):
        clear_failed_logins(client_ip)
        request.session.clear()
        request.session["logged_in"] = True
        request.session["user"] = username
        request.session["last_seen"] = time.time()
        # Regenerate CSRF token
        get_csrf_token(request)
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)

    record_failed_login(client_ip)
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "error": "Invalid username or password.",
            "csrf_token": get_csrf_token(request),
            "is_default_creds": IS_DEFAULT_CREDENTIALS,
            "default_user": APP_USERNAME,
            "default_pass": APP_PASSWORD if IS_DEFAULT_CREDENTIALS else "",
        },
        status_code=status.HTTP_401_UNAUTHORIZED,
    )


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)


# ---------- Dashboard & CRUD Routes ----------
@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    session: Session = Depends(get_session),
    current_user: str = Depends(require_login),
):
    records = session.exec(select(VehicleRecord).order_by(VehicleRecord.validity_date.asc())).all()
    today = date.today()

    urgent_count = 0
    soon_count = 0
    expired_count = 0
    active_count = 0

    enriched = []
    for r in records:
        days_left = (r.validity_date - today).days
        if days_left < 0:
            status_tag = "Expired"
            expired_count += 1
        elif days_left <= 7:
            status_tag = "Urgent"
            urgent_count += 1
        elif days_left <= 30:
            status_tag = "Soon"
            soon_count += 1
        else:
            status_tag = "OK"
            active_count += 1

        # Generate WhatsApp 1-Click Reminder message
        wa_text = (
            f"🚜 *TractorCare Insurance Renewal Reminder*\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"• *Vehicle:* {r.vehicle_model}\n"
            f"• *Registration:* {r.register_number}\n"
            f"• *Owner:* {r.owner_name}\n"
            f"• *Policy No:* {r.policy_number}\n"
            f"• *Insurance Co:* {r.insurance_company or 'Standard'}\n"
            f"• *Expires On:* {r.validity_date} ({days_left} days remaining)\n\n"
            f"⚠️ Please renew promptly to avoid penalties and policy lapse."
        )
        clean_phone = re.sub(r"[^\d]", "", r.owner_phone or "")
        if clean_phone.startswith("91") and len(clean_phone) == 12:
            wa_url = f"https://wa.me/{clean_phone}?text={quote(wa_text)}"
        elif len(clean_phone) == 10:
            wa_url = f"https://wa.me/91{clean_phone}?text={quote(wa_text)}"
        else:
            wa_url = f"https://api.whatsapp.com/send?text={quote(wa_text)}"

        enriched.append({
            "record": r,
            "days_left": days_left,
            "status": status_tag,
            "wa_url": wa_url,
        })

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "records": enriched,
            "today": today,
            "total_records": len(records),
            "urgent_count": urgent_count,
            "soon_count": soon_count,
            "expired_count": expired_count,
            "active_count": active_count,
            "csrf_token": get_csrf_token(request),
            "is_default_creds": IS_DEFAULT_CREDENTIALS,
            "is_sqlite": IS_SQLITE,
        },
    )


@app.get("/records/new", response_class=HTMLResponse)
def new_record_form(
    request: Request,
    current_user: str = Depends(require_login),
):
    return templates.TemplateResponse(
        "new_record.html",
        {"request": request, "csrf_token": get_csrf_token(request), "back": "/"},
    )


@app.post("/records/new")
def create_record(
    request: Request,
    register_number: str = Form(...),
    vehicle_model: str = Form(...),
    owner_name: str = Form(...),
    owner_phone: str = Form(""),
    address: str = Form(""),
    policy_number: str = Form(...),
    validity_date: str = Form(...),
    insurance_company: str = Form(""),
    notes: str = Form(""),
    device_token: str = Form(""),
    csrf_token: str = Form(""),
    session: Session = Depends(get_session),
    current_user: str = Depends(require_login),
):
    if not verify_csrf(request, csrf_token):
        raise HTTPException(status_code=400, detail="Invalid CSRF token")

    parsed_date = try_parse_date_string(validity_date)
    if not parsed_date:
        raise HTTPException(status_code=400, detail="Invalid date format. Expected YYYY-MM-DD or DD/MM/YYYY.")

    record = VehicleRecord(
        register_number=register_number.strip().upper(),
        vehicle_model=vehicle_model.strip(),
        owner_name=owner_name.strip(),
        owner_phone=owner_phone.strip(),
        address=address.strip(),
        policy_number=policy_number.strip().upper(),
        validity_date=parsed_date,
        insurance_company=insurance_company.strip(),
        notes=notes.strip(),
        device_token=device_token.strip(),
    )
    session.add(record)
    session.commit()
    return RedirectResponse("/?toast=Record+created+successfully", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/records/{record_id}", response_class=HTMLResponse)
def record_detail(
    record_id: int,
    request: Request,
    session: Session = Depends(get_session),
    current_user: str = Depends(require_login),
):
    record = session.get(VehicleRecord, record_id)
    if not record:
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)

    today = date.today()
    days_left = (record.validity_date - today).days

    # WhatsApp message
    wa_text = (
        f"🚜 *TractorCare Insurance Renewal Reminder*\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"• *Vehicle:* {record.vehicle_model}\n"
        f"• *Registration:* {record.register_number}\n"
        f"• *Owner:* {record.owner_name}\n"
        f"• *Policy No:* {record.policy_number}\n"
        f"• *Insurance Co:* {record.insurance_company or 'Standard'}\n"
        f"• *Expires On:* {record.validity_date} ({days_left} days remaining)\n\n"
        f"⚠️ Please renew promptly to avoid penalties and policy lapse."
    )
    clean_phone = re.sub(r"[^\d]", "", record.owner_phone or "")
    if clean_phone.startswith("91") and len(clean_phone) == 12:
        wa_url = f"https://wa.me/{clean_phone}?text={quote(wa_text)}"
    elif len(clean_phone) == 10:
        wa_url = f"https://wa.me/91{clean_phone}?text={quote(wa_text)}"
    else:
        wa_url = f"https://api.whatsapp.com/send?text={quote(wa_text)}"

    return templates.TemplateResponse(
        "record_detail.html",
        {
            "request": request,
            "record": record,
            "days_left": days_left,
            "today": today,
            "wa_url": wa_url,
            "csrf_token": get_csrf_token(request),
            "back": "/",
        },
    )


@app.get("/records/{record_id}/edit", response_class=HTMLResponse)
def edit_record_form(
    record_id: int,
    request: Request,
    session: Session = Depends(get_session),
    current_user: str = Depends(require_login),
):
    record = session.get(VehicleRecord, record_id)
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")
    return templates.TemplateResponse(
        "edit_record.html",
        {
            "request": request,
            "record": record,
            "csrf_token": get_csrf_token(request),
            "back": f"/records/{record.id}",
        },
    )


@app.post("/records/{record_id}/edit")
def edit_record_submit(
    record_id: int,
    request: Request,
    register_number: str = Form(...),
    vehicle_model: str = Form(...),
    owner_name: str = Form(...),
    owner_phone: str = Form(""),
    address: str = Form(""),
    policy_number: str = Form(...),
    validity_date: str = Form(...),
    insurance_company: str = Form(""),
    notes: str = Form(""),
    device_token: str = Form(""),
    csrf_token: str = Form(""),
    session: Session = Depends(get_session),
    current_user: str = Depends(require_login),
):
    if not verify_csrf(request, csrf_token):
        raise HTTPException(status_code=400, detail="Invalid CSRF token")

    record = session.get(VehicleRecord, record_id)
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")

    parsed_date = try_parse_date_string(validity_date)
    if not parsed_date:
        raise HTTPException(status_code=400, detail="Invalid date format.")

    record.register_number = register_number.strip().upper()
    record.vehicle_model = vehicle_model.strip()
    record.owner_name = owner_name.strip()
    record.owner_phone = owner_phone.strip()
    record.address = address.strip()
    record.policy_number = policy_number.strip().upper()
    record.validity_date = parsed_date
    record.insurance_company = insurance_company.strip()
    record.notes = notes.strip()
    record.device_token = device_token.strip()

    session.add(record)
    session.commit()
    return RedirectResponse(f"/records/{record.id}?toast=Record+updated", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/records/{record_id}/delete")
def delete_record(
    record_id: int,
    request: Request,
    csrf_token: str = Form(""),
    session: Session = Depends(get_session),
    current_user: str = Depends(require_login),
):
    if not verify_csrf(request, csrf_token):
        raise HTTPException(status_code=400, detail="Invalid CSRF token")

    record = session.get(VehicleRecord, record_id)
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")
    session.delete(record)
    session.commit()
    return RedirectResponse("/?toast=Record+deleted", status_code=status.HTTP_303_SEE_OTHER)


# ---------- OCR & Bulk Upload Endpoints ----------
@app.get("/ocr-upload", response_class=HTMLResponse)
def upload_page(
    request: Request,
    current_user: str = Depends(require_login),
):
    return templates.TemplateResponse(
        "ocr_upload.html",
        {"request": request, "csrf_token": get_csrf_token(request), "back": "/"},
    )


@app.post("/ocr-upload")
async def ocr_upload(
    request: Request,
    file: UploadFile = File(...),
    csrf_token: str = Form(""),
    current_user: str = Depends(require_login),
):
    """Detect file type, parse Excel rows or run OCR extraction safely with size guards."""
    if not verify_csrf(request, csrf_token):
        raise HTTPException(status_code=400, detail="Invalid CSRF token")

    # Read and validate file size
    raw = await file.read()
    validate_uploaded_file(file.filename or "", len(raw))
    filename = (file.filename or "").lower()

    # Excel Path: parse rows & render multi-review
    if filename.endswith((".xlsx", ".xlsm")):
        entries = parse_excel_rows(raw)
        return templates.TemplateResponse(
            "ocr_multi_result.html",
            {
                "request": request,
                "entries": entries,
                "csrf_token": get_csrf_token(request),
                "back": "/ocr-upload",
            },
        )

    # Document / Photo Path: OCR + AI / Heuristic Extraction
    extracted_text = extract_text_from_bytes(raw, filename)
    fields = ai_extract_fields_openrouter(extracted_text)

    return templates.TemplateResponse(
        "ocr_result.html",
        {
            "request": request,
            "fields": fields,
            "raw_text": extracted_text[:1500],
            "csrf_token": get_csrf_token(request),
            "back": "/ocr-upload",
        },
    )


# FIX FOR PREVIOUSLY MISSING ENDPOINT: Save Single Record Extracted from OCR
@app.post("/records/add_from_ocr")
def add_from_ocr(
    request: Request,
    register_number: str = Form(...),
    vehicle_model: str = Form(...),
    owner_name: str = Form(...),
    owner_phone: str = Form(""),
    address: str = Form(""),
    policy_number: str = Form(...),
    validity_date: str = Form(...),
    insurance_company: str = Form(""),
    notes: str = Form(""),
    csrf_token: str = Form(""),
    session: Session = Depends(get_session),
    current_user: str = Depends(require_login),
):
    if not verify_csrf(request, csrf_token):
        raise HTTPException(status_code=400, detail="Invalid CSRF token")

    parsed_date = try_parse_date_string(validity_date)
    if not parsed_date:
        raise HTTPException(status_code=400, detail="Invalid date format.")

    record = VehicleRecord(
        register_number=register_number.strip().upper(),
        vehicle_model=vehicle_model.strip(),
        owner_name=owner_name.strip(),
        owner_phone=owner_phone.strip(),
        address=address.strip(),
        policy_number=policy_number.strip().upper(),
        validity_date=parsed_date,
        insurance_company=insurance_company.strip(),
        notes=notes.strip(),
    )
    session.add(record)
    session.commit()
    return RedirectResponse("/?toast=Policy+saved+from+scan", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/excel-ai-fill")
async def excel_ai_fill(
    request: Request,
    current_user: str = Depends(require_login),
):
    """Enrich rows with guessed insurance companies."""
    body = await request.json()
    rows = body.get("rows", [])
    results = []
    for r in rows:
        if r.get("insurance_company"):
            r["ai_insurance"] = r.get("insurance_company")
        else:
            row_text = " | ".join([f"{k}:{v}" for k, v in r.items() if v])
            r["ai_insurance"] = ai_guess_insurance_for_row(row_text)
        results.append(r)
    return JSONResponse({"rows": results})


@app.post("/records/add_row")
def add_single_row(
    request: Request,
    register_number: str = Form(...),
    vehicle_model: str = Form(...),
    owner_name: str = Form(...),
    owner_phone: str = Form(""),
    address: str = Form(""),
    policy_number: str = Form(...),
    validity_date: str = Form(...),
    insurance_company: str = Form(""),
    session: Session = Depends(get_session),
    current_user: str = Depends(require_login),
):
    parsed_date = try_parse_date_string(validity_date)
    if not parsed_date:
        parsed_date = date.today()

    record = VehicleRecord(
        register_number=register_number.strip().upper(),
        vehicle_model=vehicle_model.strip(),
        owner_name=owner_name.strip(),
        owner_phone=owner_phone.strip(),
        address=address.strip(),
        policy_number=policy_number.strip().upper(),
        validity_date=parsed_date,
        insurance_company=insurance_company.strip(),
    )
    session.add(record)
    session.commit()
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/records/add_rows_bulk")
async def add_rows_bulk(
    request: Request,
    rows_json: str = Form(...),
    csrf_token: str = Form(""),
    session: Session = Depends(get_session),
    current_user: str = Depends(require_login),
):
    if not verify_csrf(request, csrf_token):
        raise HTTPException(status_code=400, detail="Invalid CSRF token")

    try:
        rows = json.loads(rows_json)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON format")

    count = 0
    for r in rows:
        vd = try_parse_date_string(r.get("validity_date", ""))
        if not vd:
            continue
        rec = VehicleRecord(
            register_number=str(r.get("register_number", "")).strip().upper(),
            vehicle_model=str(r.get("vehicle_model", "")).strip(),
            owner_name=str(r.get("owner_name", "")).strip(),
            owner_phone=str(r.get("owner_phone", "")).strip(),
            address=str(r.get("address", "")).strip(),
            policy_number=str(r.get("policy_number", "")).strip().upper(),
            validity_date=vd,
            insurance_company=str(r.get("insurance_company", "")).strip(),
        )
        session.add(rec)
        count += 1

    session.commit()
    return RedirectResponse(f"/?toast=Successfully+imported+{count}+records", status_code=status.HTTP_303_SEE_OTHER)


# ---------- Safe & Free Database Backup & Export Workarounds ----------
@app.get("/export/csv")
def export_csv(
    session: Session = Depends(get_session),
    current_user: str = Depends(require_login),
):
    """Export all records to downloadable CSV."""
    records = session.exec(select(VehicleRecord).order_by(VehicleRecord.id.asc())).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "ID", "Register Number", "Vehicle Model", "Owner Name", "Owner Phone",
        "Address", "Policy Number", "Validity Date", "Insurance Company", "Notes", "Created At"
    ])
    for r in records:
        writer.writerow([
            r.id, r.register_number, r.vehicle_model, r.owner_name, r.owner_phone,
            r.address, r.policy_number, r.validity_date.isoformat(), r.insurance_company,
            r.notes, r.created_at.isoformat() if r.created_at else ""
        ])

    csv_data = output.getvalue()
    filename = f"tractorcare_export_{date.today().isoformat()}.csv"
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/export/json")
def export_json(
    session: Session = Depends(get_session),
    current_user: str = Depends(require_login),
):
    """Export full database to downloadable JSON backup."""
    records = session.exec(select(VehicleRecord).order_by(VehicleRecord.id.asc())).all()
    data = []
    for r in records:
        data.append({
            "id": r.id,
            "register_number": r.register_number,
            "vehicle_model": r.vehicle_model,
            "owner_name": r.owner_name,
            "owner_phone": r.owner_phone,
            "address": r.address,
            "policy_number": r.policy_number,
            "validity_date": r.validity_date.isoformat(),
            "insurance_company": r.insurance_company,
            "notes": r.notes,
            "created_at": r.created_at.isoformat() if r.created_at else "",
        })

    json_data = json.dumps(data, indent=2)
    filename = f"tractorcare_backup_{date.today().isoformat()}.json"
    return Response(
        content=json_data,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------- Device Registration & Notification Testing ----------
@app.post("/register-device")
async def register_device(
    request: Request,
    session: Session = Depends(get_session),
    current_user: str = Depends(require_login),
):
    """Save device push notification token."""
    try:
        data = await request.json()
        token = data.get("token", "").strip()
        if not token:
            return JSONResponse({"ok": False, "error": "Empty token"}, status_code=400)

        existing = session.exec(select(Device).where(Device.token == token)).first()
        if not existing:
            session.add(Device(token=token))
            session.commit()
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/test-mail")
def test_mail(current_user: str = Depends(require_login)):
    """Authenticated test email trigger."""
    success = send_email(
        "🚜 TractorCare System Verification",
        f"Your TractorCare notification delivery system is active and functioning properly at {datetime.now(timezone.utc).isoformat()}."
    )
    if success:
        return {"status": "success", "message": "Verification email sent successfully."}
    return {
        "status": "notice",
        "message": "Email not delivered. Please verify SMTP_EMAIL/SMTP_PASS or MAILGUN_* environment variables in your settings.",
    }


@app.get("/test-push")
def test_push(
    session: Session = Depends(get_session),
    current_user: str = Depends(require_login),
):
    """Authenticated test push trigger."""
    record = session.exec(select(VehicleRecord).where(VehicleRecord.device_token != "")).first()
    if not record or not record.device_token:
        device = session.exec(select(Device)).first()
        if not device:
            return {"status": "notice", "message": "No device tokens registered yet. Allow push notifications on your browser/device first."}
        token = device.token
    else:
        token = record.device_token

    if not FIREBASE_ENABLED:
        return {"status": "notice", "message": "Firebase is in standby mode. Provide FIREBASE_PRIVATE_KEY in .env to activate Android/web pushes."}

    sent = send_push(token, "🚜 TractorCare Alert", "Push notifications are operational!")
    return {"status": "success" if sent else "error", "sent": sent}


# ---------- Service Workers & PWA Files ----------
@app.get("/service-worker.js", include_in_schema=False)
def get_service_worker():
    return FileResponse("service-worker.js", media_type="application/javascript")


@app.get("/firebase-messaging-sw.js", include_in_schema=False)
def get_fcm_worker():
    return FileResponse("firebase-messaging-sw.js", media_type="application/javascript")
