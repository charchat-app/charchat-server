"""7번 공통 저장·조회. 2번 조각 이외의 영역은 수정하지 않는다."""

from fastapi import APIRouter

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
):
    return service.save_creation(connection, story_id, user_id, request_id, body)
