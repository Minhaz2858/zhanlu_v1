"""Public settings router — the first endpoint the frontend calls on startup."""

from fastapi import APIRouter

router = APIRouter(tags=["public"])


@router.get("/apps/public/prod/public-settings/by-id/{app_id}")
async def get_public_settings(app_id: str):
    """Return public app settings. Always returns 200 for local dev."""
    return {
        "id": app_id,
        "public_settings": {},
    }
