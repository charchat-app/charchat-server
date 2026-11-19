"""CREATE-US-02 조회·draft 표현 저장. 인증·저장 상태·중복방지 경계."""

import hashlib
import json

import psycopg

from app.features.creation.common.errors import app_error
from app.features.creation.speech_style import repository
from app.features.creation.speech_style.schemas import (
    DEFAULT_EXPRESSION,
    ExpressionFields,
    ExpressionResponse,
)


def require_story(story):
    # 기본 정보 API와 같이 없는·남의·삭제된 스토리를 구분하지 않고 404로 돌려준다.
    if story is None or story["deleted_at"] is not None:
        raise app_error(404, "STORY_NOT_FOUND", "스토리를 찾을 수 없습니다.")


def get_expression(connection: psycopg.Connection, story_id: int, owner_id: int):
    with connection.transaction():
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        story = repository.find_story(connection, story_id, owner_id)
        require_story(story)
        fields = repository.read_expression(connection, story_id)
        return ExpressionResponse(
            story_id=story_id,
            title=story["title"],
            save_state=story["save_state"],
            saved_at=story["saved_at"],
            expression=ExpressionFields(**fields) if fields else DEFAULT_EXPRESSION,
        )


def save_expression(
    connection: psycopg.Connection,
    story_id: int,
    owner_id: int,
    request_id: str,
    fields: ExpressionFields,
):
    payload = {"story_id": story_id, "expression": fields.model_dump(mode="json")}
    request_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    with connection.transaction():
        repository.lock_request(connection, owner_id, request_id)
        story = repository.find_story(connection, story_id, owner_id, lock=True)
        require_story(story)
        previous = repository.find_request(connection, owner_id, request_id)
        if previous:
            if previous["request_hash"] != request_hash:
                raise app_error(409, "IDEMPOTENCY_KEY_REUSED", "같은 요청키의 내용이 다릅니다.")
            return ExpressionResponse.model_validate(previous["response"])
        if story["save_state"] != "draft":
            raise app_error(
                409, "FULL_SAVE_REQUIRED", "완성된 스토리는 공통 저장 흐름으로 수정해야 합니다."
            )
        saved_at = repository.write_expression(connection, story_id, payload["expression"])
        response = ExpressionResponse(
            story_id=story_id,
            title=story["title"],
            save_state=story["save_state"],
            saved_at=saved_at,
            expression=fields,
            request_id=request_id,
        )
        repository.remember_request(
            connection,
            owner_id,
            request_id,
            story_id,
            request_hash,
            response.model_dump(mode="json"),
        )
        return response
