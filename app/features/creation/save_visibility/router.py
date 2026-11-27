"""7번 공통 저장·조회. 2번 조각 이외의 영역은 수정하지 않는다."""

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response, status
from psycopg import Connection as PgConnection

from app.db.session import get_conn
from app.features.creation.category_classification.worker import classify_first
from app.features.creation.images.storage import ImageStorage, get_image_storage
from app.features.creation.save_visibility import service, soft_delete
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
    connection: Annotated[PgConnection, Depends(get_conn, scope="function")],
    storage: Annotated[ImageStorage, Depends(get_image_storage)],
    request: Request,
    background_tasks: BackgroundTasks,
):
    # 「저장」으로 사진 임시 저장본을 반영하면 연결이 끊긴 예전 파일을 지우므로 저장소가 필요하다.
    classifier = request.app.state.category_classifier
    url = request.app.state.category_database_url

    def enqueue_first() -> None:
        if url and classifier is not None:
            background_tasks.add_task(classify_first, url, story_id, classifier)

    return service.save_creation(
        connection, story_id, user_id, request_id, body, storage, on_first_commit=enqueue_first
    )


@router.delete(
    "/stories/{story_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def delete_story(story_id: StoryId, user_id: User, connection: Connection) -> Response:
    """CREATE-US-07-AC10 본인 스토리 소프트 삭제. 없음·타인·이미 삭제는 모두 404."""
    soft_delete.soft_delete_story(connection, user_id, story_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
