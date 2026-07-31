"""Authentication service — JWT token creation/verification, password hashing, and database-backed OTP/reset tokens."""

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
import jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from app.config import settings
from app.models.otp_code import OtpCode
from app.models.password_reset_token import PasswordResetToken
from app.models.refresh_token import RefreshToken
from app.models.revoked_token import RevokedToken


pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


class AuthService:

    def hash_password(self, password: str) -> str:
        return pwd_context.hash(password)

    def verify_password(self, plain: str, hashed: str) -> bool:
        try:
            return pwd_context.verify(plain, hashed)
        except Exception:
            return False

    def create_token(self, user_id: str) -> str:
        payload = {
            "sub": user_id,
            "exp": datetime.now(timezone.utc) + timedelta(hours=settings.JWT_EXPIRE_HOURS),
            "iat": datetime.now(timezone.utc),
        }
        return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)

    def verify_token(self, token: str, db: Session | None = None) -> str | None:
        try:
            payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
            return None
        # Access tokens carry a JTI; check the revocation blacklist when a DB
        # session is available. Refresh tokens have no JTI and are validated
        # via verify_refresh_token against the refresh_tokens table instead.
        if db is not None and payload.get("type") == "access":
            jti = payload.get("jti")
            if jti and self.is_access_token_revoked(jti, db):
                return None
        return payload.get("sub")

    # --- Access / refresh token lifecycle (plan 2026-07-27) ---

    def create_access_token(self, user_id: str, db: Session) -> str:
        """Issue a short-lived (ACCESS_TOKEN_EXPIRE_MINUTES) access JWT with a JTI."""
        jti = str(uuid.uuid4())
        payload = {
            "sub": user_id,
            "jti": jti,
            "type": "access",
            "exp": datetime.now(timezone.utc) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
            "iat": datetime.now(timezone.utc),
        }
        return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)

    def create_refresh_token(self, user_id: str, db: Session) -> str:
        """Issue a long-lived refresh token. Stores a SHA-256 hash; returns the RAW token (seen only by the client)."""
        raw = secrets.token_urlsafe(48)
        record = RefreshToken(
            user_id=user_id,
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            expires_at=datetime.utcnow() + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
        )
        db.add(record)
        db.commit()
        return raw

    def verify_refresh_token(self, token: str, db: Session) -> str | None:
        """Return user_id if the refresh token is valid, unused, and unexpired; else None."""
        h = hashlib.sha256(token.encode()).hexdigest()
        record = db.query(RefreshToken).filter(
            RefreshToken.token_hash == h,
            RefreshToken.used == False,  # noqa: E712
            RefreshToken.is_deleted == False,  # noqa: E712
        ).first()
        if not record:
            return None
        if datetime.utcnow() > record.expires_at:
            return None
        return record.user_id

    def rotate_refresh_token(self, old_token: str, db: Session) -> tuple[str, str] | None:
        """Mark the old refresh token used and issue a fresh (access, refresh) pair.

        Returns None if the old token is invalid/expired/replayed. On replay of an
        already-used token we do NOT know whose it is (the hash lookup returns
        nothing usable), so we simply reject — the client must re-authenticate.
        """
        user_id = self.verify_refresh_token(old_token, db)
        if not user_id:
            return None
        h = hashlib.sha256(old_token.encode()).hexdigest()
        record = db.query(RefreshToken).filter(RefreshToken.token_hash == h).first()
        record.used = True
        db.commit()
        access = self.create_access_token(user_id, db)
        refresh = self.create_refresh_token(user_id, db)
        return access, refresh

    def revoke_access_token(self, jti: str, user_id: str, exp: datetime, db: Session) -> None:
        """Add an access token's JTI to the blacklist (used on logout)."""
        db.add(RevokedToken(jti=jti, user_id=user_id, expires_at=exp))
        db.commit()

    def is_access_token_revoked(self, jti: str, db: Session) -> bool:
        return db.query(RevokedToken).filter(RevokedToken.jti == jti).first() is not None

    def revoke_all_user_refresh_tokens(self, user_id: str, db: Session) -> None:
        """Invalidate every unused refresh token for a user (logout / password change)."""
        db.query(RefreshToken).filter(
            RefreshToken.user_id == user_id,
            RefreshToken.used == False,  # noqa: E712
            RefreshToken.is_deleted == False,  # noqa: E712
        ).update({RefreshToken.used: True})
        db.commit()

    # --- OTP (database-backed) ---

    def generate_otp(self, email: str, db: Session) -> str:
        """Generate a 6-digit OTP, store it in the database, and return it.

        Uses ``secrets.randbelow`` (cryptographic RNG) instead of
        ``random.randint`` (Mersenne Twister, predictable). Also
        invalidates any previous unused OTPs for this email so only
        the most recent code is valid (prevents OTP accumulation attacks).
        """
        # Invalidate all previous unused OTPs for this email
        db.query(OtpCode).filter(
            OtpCode.email == email,
            OtpCode.used == False,  # noqa: E712
            OtpCode.is_deleted == False,  # noqa: E712
        ).update({OtpCode.used: True})

        otp = str(100000 + secrets.randbelow(900000))
        record = OtpCode(
            email=email,
            code=otp,
            expires_at=datetime.utcnow() + timedelta(minutes=10),
        )
        db.add(record)
        db.commit()
        return otp

    def verify_otp(self, email: str, otp_code: str, db: Session) -> bool:
        """Verify an OTP code against the database. Marks it as used on success."""
        record = db.query(OtpCode).filter(
            OtpCode.email == email,
            OtpCode.code == otp_code,
            OtpCode.used == False,
            OtpCode.is_deleted == False,
        ).order_by(OtpCode.created_date.desc()).first()

        if not record:
            return False
        if datetime.utcnow() > record.expires_at:
            return False

        record.used = True
        db.commit()
        return True

    # --- Password reset tokens (database-backed) ---

    def create_reset_token(self, user_id: str, db: Session) -> str:
        """Create a secure password reset token and store it in the database."""
        token = secrets.token_urlsafe(32)
        record = PasswordResetToken(
            user_id=user_id,
            token=token,
            expires_at=datetime.utcnow() + timedelta(hours=1),
        )
        db.add(record)
        db.commit()
        return token

    def verify_reset_token(self, token: str, db: Session) -> str | None:
        """Verify a password reset token and return the user_id if valid."""
        record = db.query(PasswordResetToken).filter(
            PasswordResetToken.token == token,
            PasswordResetToken.used == False,
            PasswordResetToken.is_deleted == False,
        ).first()

        if not record:
            return None
        if datetime.utcnow() > record.expires_at:
            return None

        return record.user_id

    def consume_reset_token(self, token: str, db: Session):
        """Mark a reset token as used after successful password reset."""
        record = db.query(PasswordResetToken).filter(
            PasswordResetToken.token == token,
        ).first()
        if record:
            record.used = True
            db.commit()


auth_service = AuthService()
