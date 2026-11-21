"""7번 공통 저장·조회. 2번 조각 이외의 영역은 수정하지 않는다."""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.features.creation.images.storage import ImageStorage, get_image_storage
from app.features.creation.save_visibility import service
from app.features.creation.save_visibility.schemas import CreationSaveRequest, CreationSaveResponse
from app.features.creation.speech_style.router import Connection, RequestId, StoryId, User

router = APIRouter(tags=["creation:save_visibility"])


@router.get("/stories/{story_id}/creation", response_model=CreationSaveResponse)
def get_creation(story_id: StoryId, user_id: User, connection: Connection):
    return service.get_creation(connection, story_id, user_id)


@router.put("/stories/{story_id}/creation", response_model=CreationSaveResponse)
def save_creation(
    story_id: StoryId,
    body: CreationSaveRequest,
    request_id: RequestId,
    user_id: User,
    connection: Connection,
    storage: Annotated[ImageStorage, Depends(get_image_storage)],
):
    # 「저장」으로 사진 임시 저장본을 반영하면 연결이 끊긴 예전 파일을 지우므로 저장소가 필요하다.
    return service.save_creation(connection, story_id, user_id, request_id, body, storage)
