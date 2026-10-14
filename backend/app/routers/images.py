"""Image serving endpoint."""

import os

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from app.config import settings
from app.validators import valid_qid

router = APIRouter(prefix="/api/images", tags=["images"])


@router.get("/{qid}.jpg")
async def serve_image(qid: str = Depends(valid_qid)):
    """Serve a cached optimized image by QID."""
    path = os.path.join(settings.image_cache_dir, f"{qid}.jpg")
    if not os.path.isfile(path):
        raise HTTPException(404, f"Image for {qid} not found")
    return FileResponse(path, media_type="image/jpeg")
