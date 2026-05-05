import re
from typing import Any, Dict, Optional, Union

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from app.handlers.response_handler import ResponseHandler
from app.resources.errors import CRASH
from app.routers.hentai20.hentai20 import (
     get_panels,
     get_manga,
     download_image_from_url,
     get_filter_mangas
)

router: APIRouter = APIRouter(prefix="/hentai")
response: ResponseHandler = ResponseHandler()

SLUG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_./-]{0,180}$")
ALLOWED_STATUS = {"ongoing", "completed", "complete", "hiatus"}
ALLOWED_TYPES = {"manga", "manhwa", "manhua", "comic"}
ALLOWED_SORTS = {"latest", "popular", "update", "az", "za", "rating"}


def is_valid_slug(value: str) -> bool:
     return bool(value and SLUG_RE.fullmatch(value)) and ".." not in value


@router.get("/proxy/{image_url:path}")
def proxy(image_url: Optional[str] = None):
     image_bytes = download_image_from_url(image_url)

     if not image_bytes:
          return FileResponse("media/error.gif", media_type="image/gif")

     return Response(content=image_bytes, media_type="image/jpeg")


@router.get("/filter")
async def filter_mangas(
     page: int = Query(default=1, ge=1, le=500),
     genre: Optional[str] = Query(default=None, max_length=80),
     status: Optional[str] = Query(default=None, max_length=40),
     _type: Optional[str] = Query(default=None, max_length=40),
     sort: Optional[str] = Query(default=None, max_length=40),
     ) -> JSONResponse:
     params = {"page": str(page)}

     if status:
          normalized_status = status.lower().strip()
          if normalized_status not in ALLOWED_STATUS:
               raise HTTPException(status_code=400, detail="Invalid status parameter")
          params["status"] = normalized_status

     if _type:
          normalized_type = _type.lower().strip()
          if normalized_type not in ALLOWED_TYPES:
               raise HTTPException(status_code=400, detail="Invalid type parameter")
          params["type"] = normalized_type

     if sort:
          normalized_sort = sort.lower().strip()
          if normalized_sort not in ALLOWED_SORTS:
               raise HTTPException(status_code=400, detail="Invalid sort parameter")
          params["order"] = normalized_sort

     if genre:
          if not re.fullmatch(r"[a-zA-Z0-9 _-]{1,80}", genre):
               raise HTTPException(status_code=400, detail="Invalid genre parameter")
          params["genre[]"] = genre

     data: Union[Dict[str, Any], int] = await get_filter_mangas(endpoint="/manga/", params=params)

     if data == CRASH or type(data) is int:
          return response.bad_request_response()

     return response.successful_response({"data": data})


@router.get("/read/{chapter_id:path}")
async def read(chapter_id: str) -> JSONResponse:
     if not is_valid_slug(chapter_id):
          raise HTTPException(status_code=400, detail="Invalid chapter_id")

     data: Union[Dict[str, Any], int] = await get_panels(chapter_id=chapter_id)

     if data == CRASH:
          return response.bad_request_response()

     return response.successful_response({"data": data})


@router.get("/{manga_id}")
async def manga(manga_id: str) -> JSONResponse:
     if not is_valid_slug(manga_id):
          raise HTTPException(status_code=400, detail="Invalid manga_id")

     data: Union[Dict[str, Any], int] = await get_manga(manga_id=manga_id)

     if data == CRASH:
          return response.bad_request_response()

     return response.successful_response({"data": data})
