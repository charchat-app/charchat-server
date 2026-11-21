"""CREATE-US-06 이미지 업로드·파일 조회·스토리/캐릭터 이미지 목록 API."""

from typing import Annotated

from fastapi import APIRouter, Depends, File, Header, Path, Query, UploadFile
from fastapi.responses import FileResponse

from app.api.deps import get_current_user_id
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


def get_optional_user_id(
    x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
) -> int | None:
    """공개 이미지는 로그인 없이 볼 수 있어 사용자 헤더를 선택으로 받는다.

    헤더를 보냈는데 형식이 틀리면 공통 인증과 같게 401로 거절한다.
    """
    if x_user_id is None:
        return None
    return get_current_user_id(x_user_id)


Viewer = Annotated[int | None, Depends(get_optional_user_id)]

# 공개 이미지: 저장 키는 내용이 바뀌지 않으므로(교체는 새 파일) 오래 캐시해도 된다.
PUBLIC_CACHE = "public, max-age=31536000, immutable"
# 소유자 전용 이미지: 공유 캐시(프록시·CDN)나 디스크에 남기지 않는다.
PRIVATE_CACHE = "private, no-store"


@router.get("/image-files/{storage_key}", response_class=FileResponse)
def get_image_file(
    storage_key: Annotated[str, Path(max_length=64)],
    viewer_id: Viewer,
    connection: Connection,
    storage: Storage,
):
    found = service.find_file(connection, storage, storage_key, viewer_id)
    return FileResponse(
        found.path,
        media_type=found.mime_type,
        headers={
            "Cache-Control": PUBLIC_CACHE if found.is_public else PRIVATE_CACHE,
            # 같은 주소라도 사용자에 따라 응답이 달라질 수 있음을 캐시에 알린다.
            "Vary": "X-User-Id",
        },
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
):
    return service.save_story_images(connection, story_id, user_id, request_id, body)
