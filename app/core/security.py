"""
Security layer: JWT authentication and API-key validation.

Exposes two FastAPI dependencies:
- `get_current_user`  — validates a Bearer JWT, returns the authenticated User.
- `verify_api_key`    — validates an X-API-Key header for external integrations.
"""

import hashlib
import hmac
from datetime import UTC, datetime

import redis
from fastapi import Header, HTTPException, Request, status
from passlib.context import CryptContext

from app.core.config import get_settings

settings = get_settings()

# ── Password hashing ────────────────────────────────────
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    """Return a bcrypt hash of the given plaintext password."""
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a plaintext password against its bcrypt hash."""
    return pwd_context.verify(plain, hashed)


from pathlib import Path

TOKEN_FILE_PATH = Path("/app/access_token.hash")

# Redis connection for sessions
redis_client = redis.Redis(host="redis", port=6379, db=0, decode_responses=True)


def verify_access_token(token: str) -> bool:
    """Hash the input token and compare it with the stored hash using constant-time comparison."""
    if not token:
        return False
    try:
        if TOKEN_FILE_PATH.is_file():
            stored_hash = TOKEN_FILE_PATH.read_text().strip()
            input_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
            return hmac.compare_digest(input_hash, stored_hash)
    except Exception:
        pass
    return False


class OwnerUser:
    """Statically defined Owner user representing the single occupant of this app."""

    id = 1
    username = "owner"
    email = "owner@netsanctum.local"
    is_active = True
    is_superuser = True

    @classmethod
    def model_validate(cls, obj):
        # Compatibility helper for Pydantic serialization
        return {
            "id": 1,
            "username": "owner",
            "email": "owner@netsanctum.local",
            "is_active": True,
            "is_superuser": True,
            "created_at": datetime.now(UTC),
        }


# ── FastAPI dependencies ─────────────────────────────────
async def get_current_user(request: Request):
    """
    Validate the session cookie or the Bearer token.
    Returns OwnerUser() if valid, otherwise raises HTTPException.
    """
    token = None

    # 1. Check Bearer token (API)
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1]
        if verify_access_token(token):
            return OwnerUser()

    # 2. Check Session Cookie (UI)
    session_id = request.cookies.get("access_token")
    if session_id:
        user_data = redis_client.get(f"session:{session_id}")
        if user_data == "1":
            return OwnerUser()

    # 3. Check Query Parameter (for external players like VLC/mpv)
    query_token = request.query_params.get("token")
    if query_token:
        # Check if it is a raw master token
        if verify_access_token(query_token):
            return OwnerUser()
        # Or check if it is a session ID
        user_data = redis_client.get(f"session:{query_token}")
        if user_data == "1":
            return OwnerUser()

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid access token or session",
    )


async def verify_api_key(
    x_api_key: str = Header(..., alias="X-API-Key"),
) -> str:
    """
    Validate the X-API-Key header against the master key.

    Returns the key on success so downstream handlers can log it.
    """
    if x_api_key != settings.MASTER_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API key",
        )
    return x_api_key
