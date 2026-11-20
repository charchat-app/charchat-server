"""CREATE-US-06 이미지 업로드·파일 조회·스토리/캐릭터 이미지 목록 API."""

from typing import Annotated

from fastapi import APIRouter, Depends, File, Path, Query, UploadFile
from fastapi.responses import FileResponse

from app.features.creation.images import service
from app.features.creation.images.schemas import (
    MAX_IMAGE_BYTES,
    ImageItem,
    ImageTarget,
    StoryImagesResponse,
    StoryImagesSaveRequest,
)
from app.features.creation.images.storage import ImageStorage, get_image_storage
from app.features.creation.speech_style.router import Connection, RequestId, StoryId, User

router = APIRouter(tags=["creation:images"])
Storage = Annotated[ImageStorage, Depends(get_image_storage)]


@router.post("/images", response_model=ImageItem, status_code=201)
def upload_image(
    file: Annotated[UploadFile, File()],
    target: Annotated[ImageTarget, Query()],
    user_id: User,
    connection: Connection,
    storage: Storage,
):
    # 5MB를 넘는지만 알면 되므로 한도보다 1바이트만 더 읽는다.
    data = file.file.read(MAX_IMAGE_BYTES + 1)
    return service.upload_image(connection, storage, user_id, target, data)


@router.get("/image-files/{storage_key}", response_class=FileResponse)
def get_image_file(
    storage_key: Annotated[str, Path(max_length=64)],
    connection: Connection,
    storage: Storage,
):
    path, mime_type = service.find_file(connection, storage, storage_key)
    # 저장 키는 내용이 바뀌지 않으므로(교체는 새 파일) 오래 캐시해도 된다.
    return FileResponse(
        path,
        media_type=mime_type,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@router.get("/stories/{story_id}/images", response_model=StoryImagesResponse)
def get_story_images(story_id: StoryId, user_id: User, connection: Connection):
    return service.get_story_images(connection, story_id, user_id)


@router.put("/stories/{story_id}/images", response_model=StoryImagesResponse)
def save_story_images(
    story_id: StoryId,
    body: StoryImagesSaveRequest,
    request_id: RequestId,
    user_id: User,
    connection: Connection,
    storage: Storage,
):
    return service.save_story_images(connection, story_id, user_id, request_id, body, storage)
