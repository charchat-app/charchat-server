"""CREATE-US-02 퓨샷 조회·컬렉션 저장·참조 검증·요청 중복방지."""

import hashlib
import json

from app.features.creation.common.errors import app_error
from app.features.creation.speech_style import fewshot_repository as repo
from app.features.creation.speech_style import repository
from app.features.creation.speech_style.fewshot_schemas import (
    FewshotCharactersResponse,
    FewshotsResponse,
)
from app.features.creation.speech_style.service import require_story


def characters(connection, story_id):
    return [dict(row) for row in repo.read_characters(connection, story_id)]


def get_characters(connection, story_id, owner_id):
    """편집 본문을 재조회하지 않는 소유자 전용 읽기 투영."""
    with connection.transaction():
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        require_story(repository.find_story(connection, story_id, owner_id))
        return FewshotCharactersResponse(
            story_id=story_id, characters=characters(connection, story_id)
        )


def snapshot(story, groups, people, request_id=None):
    return FewshotsResponse(
        story_id=story["id"],
        title=story["title"],
        save_state=story["save_state"],
        saved_at=story["saved_at"],
        characters=people,
        fewshots=groups,
        request_id=request_id,
    )


def get_fewshots(connection, story_id, owner_id):
    with connection.transaction():
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        story = repository.find_story(connection, story_id, owner_id)
        require_story(story)
        return read_snapshot(connection, story)


def read_snapshot(connection, story):
    story_id = story["id"]
    messages = repo.read_messages(connection, story_id)
    grouped = {}
    for row in messages:
        grouped.setdefault(row["fewshot_id"], []).append(
            {
                "id": row["id"],
                "client_key": f"message-{row['id']}",
                "speaker_kind": row["speaker_kind"],
                "character_id": row["character_id"],
                "body": row["body"],
            }
        )
    groups = [
        {
            "id": g["id"],
            "client_key": f"group-{g['id']}",
            "messages": grouped.get(g["id"], []),
        }
        for g in repo.read_groups(connection, story_id)
    ]
    return snapshot(story, groups, characters(connection, story_id))


def save_fewshots(connection, story_id, owner_id, request_id, body):
    payload = {"operation": "fewshots", "story_id": story_id, **body.model_dump(mode="json")}
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    with connection.transaction():
        repository.lock_request(connection, owner_id, request_id)
        story = repository.find_story(connection, story_id, owner_id, lock=True)
        require_story(story)
        previous = repository.find_request(connection, owner_id, request_id)
        if previous:
            if previous["request_hash"] != digest:
                raise app_error(409, "IDEMPOTENCY_KEY_REUSED", "같은 요청키의 내용이 다릅니다.")
            return FewshotsResponse.model_validate(previous["response"])
        if story["save_state"] != "draft":
            raise app_error(
                409, "FULL_SAVE_REQUIRED", "완성된 스토리는 공통 저장 흐름으로 수정해야 합니다."
            )
        groups, people = write_fewshots(connection, story_id, body)
        story["saved_at"] = repository.touch_saved_at(connection, story_id)
        response = snapshot(story, groups, people, request_id)
        repository.remember_request(
            connection, owner_id, request_id, story_id, digest, response.model_dump(mode="json")
        )
        return response


def write_fewshots(connection, story_id, body):
    people = characters(connection, story_id)
    character_ids = {p["id"] for p in people}
    old_groups = {g["id"] for g in repo.read_groups(connection, story_id)}
    old_messages = {m["id"]: m["fewshot_id"] for m in repo.read_messages(connection, story_id)}
    for group in body.fewshots:
        if group.id is not None and group.id not in old_groups:
            raise app_error(422, "INVALID_FEWSHOT_REFERENCE", "예시의 연결 정보를 확인해 주세요.")
        for message in group.messages:
            if message.id is not None and (
                message.id not in old_messages
                or group.id is None
                or old_messages[message.id] != group.id
            ):
                raise app_error(
                    422, "MESSAGE_GROUP_CHANGED", "말풍선은 다른 예시로 옮길 수 없습니다."
                )
            if message.character_id is not None and message.character_id not in character_ids:
                raise app_error(422, "INVALID_SPEAKER", "같은 스토리의 인물을 선택해 주세요.")
    groups = repo.write_collection(
        connection,
        story_id,
        body.fewshots,
        old_groups,
        set(old_messages),
    )
    return groups, people
