"""CREATE-US-02 표현 설정 조회와 임시 저장 API."""

from typing import Annotated

from fastapi import APIRouter, Header, Path

from app.api.deps import CurrentUserId, DbConn
from app.features.creation.speech_style import fewshot_service, service
from app.features.creation.speech_style.fewshot_schemas import (
    FewshotCharactersResponse,
    FewshotsResponse,
    FewshotsSaveRequest,
)
from app.features.creation.speech_style.schemas import ExpressionResponse, ExpressionSaveRequest

router = APIRouter(tags=["creation:speech_style"])
StoryId = Annotated[int, Path(gt=0, le=9223372036854775807)]
# 사용자 구분과 DB 연결은 공통 의존성(app.api.deps)을 그대로 쓴다.
User = CurrentUserId
Connection = DbConn
RequestId = Annotated[
    str, Header(alias="Idempotency-Key", min_length=1, max_length=120, pattern=r"^[a-zA-Z0-9_-]+$")
]


@router.get(
    "/stories/{story_id}/expression",
    response_model=ExpressionResponse,
)
def get_expression(story_id: StoryId, user_id: User, connection: Connection):
    return service.get_expression(connection, story_id, user_id)


@router.put(
    "/stories/{story_id}/expression",
    response_model=ExpressionResponse,
)
def save_expression(
    story_id: StoryId,
    body: ExpressionSaveRequest,
    request_id: RequestId,
    user_id: User,
    connection: Connection,
):
    return service.save_expression(
        connection,
        story_id,
        user_id,
        request_id,
        body.expression,
    )


@router.get("/stories/{story_id}/fewshots", response_model=FewshotsResponse)
def get_fewshots(story_id: StoryId, user_id: User, connection: Connection):
    return fewshot_service.get_fewshots(connection, story_id, user_id)


@router.get("/stories/{story_id}/fewshot-characters", response_model=FewshotCharactersResponse)
def get_fewshot_characters(story_id: StoryId, user_id: User, connection: Connection):
    return fewshot_service.get_characters(connection, story_id, user_id)


@router.put("/stories/{story_id}/fewshots", response_model=FewshotsResponse)
def save_fewshots(
    story_id: StoryId,
    body: FewshotsSaveRequest,
    request_id: RequestId,
    user_id: User,
    connection: Connection,
):
    return fewshot_service.save_fewshots(connection, story_id, user_id, request_id, body)
