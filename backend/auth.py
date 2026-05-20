"""JWT auth utilities for DevFleet online."""
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

from jose import jwt, JWTError
from passlib.context import CryptContext

import db

log = logging.getLogger("devfleet")

SECRET_KEY = os.environ.get("DEVFLEET_JWT_SECRET", "change-me-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 24

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")

if SECRET_KEY == "change-me-in-production":
    log.warning("DEVFLEET_JWT_SECRET is default — set a strong secret before exposing to internet")


def hash_password(plain: str) -> str:
    if len(plain.encode("utf-8")) > 72:
        raise ValueError("Password must be 72 bytes or fewer (bcrypt limit)")
    return _pwd.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return _pwd.verify(plain, hashed)


def create_access_token(user_id: str, email: str, role: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    return jwt.encode(
        {"sub": user_id, "email": email, "role": role, "exp": expire},
        SECRET_KEY, algorithm=ALGORITHM,
    )


def decode_token(token: str) -> dict:
    """Decode and validate a JWT. Raises JWTError on invalid/expired."""
    return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])


async def get_user_by_email(email: str) -> dict | None:
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall("SELECT * FROM users WHERE email=?", (email,))
        return dict(rows[0]) if rows else None
    finally:
        await conn.close()


async def create_user(email: str, password: str, role: str = "user") -> dict:
    user_id = str(uuid.uuid4())
    conn = await db.get_db()
    try:
        await conn.execute(
            "INSERT INTO users (id, email, password_hash, role) VALUES (?,?,?,?)",
            (user_id, email, hash_password(password), role),
        )
        await conn.commit()
        return {"id": user_id, "email": email, "role": role}
    finally:
        await conn.close()


async def create_invite_token(created_by: str) -> str:
    token = str(uuid.uuid4())
    expire = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    conn = await db.get_db()
    try:
        await conn.execute(
            "INSERT INTO invite_tokens (token, created_by, expires_at) VALUES (?,?,?)",
            (token, created_by, expire),
        )
        await conn.commit()
        return token
    finally:
        await conn.close()


async def consume_invite_token(token: str, used_by: str) -> bool:
    """Mark token used. Returns False if invalid/expired/already-used."""
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(
            "SELECT token FROM invite_tokens WHERE token=? AND used_by IS NULL AND expires_at > datetime('now')",
            (token,),
        )
        if not rows:
            return False
        await conn.execute(
            "UPDATE invite_tokens SET used_by=?, used_at=datetime('now') WHERE token=?",
            (used_by, token),
        )
        await conn.commit()
        return True
    finally:
        await conn.close()
