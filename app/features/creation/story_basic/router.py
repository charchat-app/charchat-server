"""CREATE-US-01 엔드포인트. 스토리 기본 정보·세계관·해시태그 저장과 재조회."""

from fastapi import APIRouter

from app.api.deps import CurrentUserId, DbConn
from app.features.creation.story_basic import service
from app.features.creation.story_basic.schemas import (
    StoryCreateRequest,
    StoryResponse,
    StorySummaryResponse,
    StoryUpdateRequest,
)

router = APIRouter(tags=["creation:story_basic"])


@router.post("/stories", status_code=201, response_model=StoryResponse)
def create_story(body: StoryCreateRequest, user_id: CurrentUserId, conn: DbConn) -> StoryResponse:
    """새 스토리를 임시 저장 상태로 만든다."""
    return service.create_story(conn, user_id, body)


@router.get("/stories", response_model=list[StorySummaryResponse])
def list_stories(user_id: CurrentUserId, conn: DbConn) -> list[StorySummaryResponse]:
    """내 스토리 목록. 삭제된 스토리는 제외하고 최근 저장 순으로 돌려준다."""
    return service.list_stories(conn, user_id)


@router.get("/stories/{story_id}", response_model=StoryResponse)
def get_story(story_id: int, user_id: CurrentUserId, conn: DbConn) -> StoryResponse:
    """마지막으로 저장된 기본 정보를 돌려준다."""
    return service.get_story(conn, user_id, story_id)


@router.put("/stories/{story_id}", response_model=StoryResponse)
def update_story(
    story_id: int, body: StoryUpdateRequest, user_id: CurrentUserId, conn: DbConn
) -> StoryResponse:
    """기본 정보를 저장한다."""
    return service.update_story(conn, user_id, story_id, body)
