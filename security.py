# security.py
import os
import time
import secrets
from typing import Dict, Tuple
from fastapi import Request, HTTPException, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

# Rate limiting storage: { ip: [timestamp_1, timestamp_2, ...] }
_FAILED_LOGINS: Dict[str, list] = {}
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_PERIOD_SECONDS = 300  # 5 minutes

# Default admin credentials if none configured in environment
APP_USERNAME = os.getenv("APP_USERNAME", "admin").strip()
APP_PASSWORD = os.getenv("APP_PASSWORD", "tractorcare2026").strip()
IS_DEFAULT_CREDENTIALS = (
    os.getenv("APP_USERNAME") is None or os.getenv("APP_PASSWORD") is None
)

# Persistent session secret
def get_or_create_session_secret() -> str:
    env_secret = os.getenv("SESSION_SECRET_KEY", "").strip()
    if env_secret and env_secret != "fallback-secret-key":
        return env_secret
    
    secret_file = os.path.join(os.path.dirname(__file__), ".session_secret")
    if os.path.exists(secret_file):
        try:
            with open(secret_file, "r") as f:
                content = f.read().strip()
                if len(content) >= 32:
                    return content
        except Exception:
            pass

    new_secret = secrets.token_hex(32)
    try:
        with open(secret_file, "w") as f:
            f.write(new_secret)
    except Exception:
        pass
    return new_secret

SESSION_SECRET_KEY = get_or_create_session_secret()


# Rate limiter for authentication
def check_login_rate_limit(client_ip: str) -> Tuple[bool, int]:
    """Check if client IP is currently rate limited. Returns (is_allowed, seconds_remaining)."""
    now = time.time()
    attempts = _FAILED_LOGINS.get(client_ip, [])
    # Filter attempts within lockout window
    recent_attempts = [t for t in attempts if now - t < LOCKOUT_PERIOD_SECONDS]
    _FAILED_LOGINS[client_ip] = recent_attempts

    if len(recent_attempts) >= MAX_LOGIN_ATTEMPTS:
        oldest_attempt = min(recent_attempts)
        remaining = int(LOCKOUT_PERIOD_SECONDS - (now - oldest_attempt))
        return False, max(1, remaining)
    return True, 0


def record_failed_login(client_ip: str):
    now = time.time()
    if client_ip not in _FAILED_LOGINS:
        _FAILED_LOGINS[client_ip] = []
    _FAILED_LOGINS[client_ip].append(now)


def clear_failed_logins(client_ip: str):
    if client_ip in _FAILED_LOGINS:
        del _FAILED_LOGINS[client_ip]


def verify_credentials(username: str, password: str) -> bool:
    """Constant-time comparison to prevent timing attacks."""
    if not username or not password:
        return False
    is_user_valid = secrets.compare_digest(username.strip(), APP_USERNAME)
    is_pass_valid = secrets.compare_digest(password.strip(), APP_PASSWORD)
    return is_user_valid and is_pass_valid


# CSRF Protection
def get_csrf_token(request: Request) -> str:
    if "session" not in request.scope:
        return ""
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


def verify_csrf(request: Request, submitted_token: str) -> bool:
    if "session" not in request.scope:
        return False
    session_token = request.session.get("csrf_token", "")
    if not session_token or not submitted_token:
        return False
    return secrets.compare_digest(session_token, submitted_token)


# Security Headers Middleware
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        return response


# File upload validator
MAX_FILE_SIZE = 15 * 1024 * 1024  # 15 MB
ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".xlsx", ".xlsm", ".csv"}

def validate_uploaded_file(filename: str, file_size: int):
    if not filename:
        raise HTTPException(status_code=400, detail="No file selected")
    
    ext = os.path.splitext(filename.lower())[1]
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file format '{ext}'. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
        )
    
    if file_size > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds maximum allowed size of 15 MB."
        )
