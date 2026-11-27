"""CREATE-US-01 엔드포인트. 스토리 기본 정보·세계관·해시태그 저장과 재조회."""

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import CommonSafetyDep, CurrentUserId, DbConn
from app.features.creation.story_basic import service
from app.features.creation.story_basic.schemas import (
    PublicStorySummaryResponse,
    StoryCreateRequest,
    StoryResponse,
    StorySummaryResponse,
    StoryUpdateRequest,
    TagSuggestionResponse,
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


@router.get("/tags", response_model=list[TagSuggestionResponse])
def suggest_tags(
    q: Annotated[str, Query(min_length=1, max_length=100, description="입력 중인 해시태그")],
    user_id: CurrentUserId,
    conn: DbConn,
) -> list[TagSuggestionResponse]:
    """해시태그 자동검색 후보와 공개 스토리 수 (CREATE-US-01-AC17)."""
    return service.suggest_tags(conn, q)


@router.get("/tags/stories", response_model=list[PublicStorySummaryResponse])
def search_stories_by_tag(
    tag: Annotated[str, Query(min_length=1, max_length=100, description="찾을 해시태그")],
    user_id: CurrentUserId,
    conn: DbConn,
    guard: CommonSafetyDep,
) -> list[PublicStorySummaryResponse]:
    """해시태그가 붙은 공개 스토리 (CREATE-US-01-AC16). 화면은 탐색 기능에서 연결한다.

    태그에 '/' 등이 들어가도 찾을 수 있게 경로가 아니라 쿼리(?tag=)로 받는다.
    """
    return service.search_public_stories_by_tag(conn, tag, guard)
