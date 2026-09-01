"""Public settings router — the first endpoint the frontend calls on startup."""

from fastapi import APIRouter

from app.config import settings

router = APIRouter(tags=["public"])


@router.get("/apps/public/prod/public-settings/by-id/{app_id}")
async def get_public_settings(app_id: str):
    """Return public app settings. Always returns 200 for local dev."""
    return {
        "id": app_id,
        "public_settings": {
            # Surfaced to the frontend so it can hide the "Create account"
            # link / register page when self-registration is disabled
            # (enterprise provisioning model — admins create accounts).
            "allow_public_registration": settings.ALLOW_PUBLIC_REGISTRATION,
        },
    }
