"""CREATE-US-03 인트로 조회·컬렉션 저장·참조 검증·완성 저장 조건."""

from app.features.creation.common.errors import app_error
from app.features.creation.intro import repository as repo
from app.features.creation.intro.schemas import INTRO_BODY_LIMIT, INTRO_TITLE_LIMIT


def read_intros(connection, story_id):
    grouped = {}
    for row in repo.read_messages(connection, story_id):
        grouped.setdefault(row["intro_id"], []).append(
            {
                "id": str(row["id"]),
                "client_key": f"intro-message-{row['id']}",
                "speaker_kind": row["speaker_kind"],
                "character_id": str(row["character_id"])
                if row["character_id"] is not None
                else None,
                "body": row["body"],
            }
        )
    return [
        {
            "id": str(intro["id"]),
            "client_key": f"intro-{intro['id']}",
            "title": intro["title"],
            "messages": grouped.get(intro["id"], []),
        }
        for intro in repo.read_intros(connection, story_id)
    ]


def write_intros(connection, story_id, intros, character_ids):
    old_intros = {str(i["id"]) for i in repo.read_intros(connection, story_id)}
    old_messages = {
        str(m["id"]): str(m["intro_id"]) for m in repo.read_messages(connection, story_id)
    }
    for intro in intros:
        if intro.id is not None and intro.id not in old_intros:
            raise app_error(422, "INVALID_INTRO_REFERENCE", "인트로의 연결 정보를 확인해 주세요.")
        for message in intro.messages:
            if message.id is not None and (
                message.id not in old_messages
                or intro.id is None
                or old_messages[message.id] != intro.id
            ):
                raise app_error(
                    422, "MESSAGE_INTRO_CHANGED", "말풍선은 다른 인트로로 옮길 수 없습니다."
                )
            if message.character_id is not None and message.character_id not in character_ids:
                raise app_error(422, "INVALID_SPEAKER", "같은 스토리의 인물을 선택해 주세요.")
    return repo.write_collection(
        connection,
        story_id,
        intros,
        {int(i) for i in old_intros},
        {int(m) for m in old_messages},
    )


def validate_completed(intros):
    """공개·비공개 저장 조건. Unicode 코드 포인트 단위, 원문을 바꾸지 않는다."""
    if not intros:
        raise app_error(422, "INTRO_REQUIRED", "인트로를 1개 이상 작성해 주세요.")
    if any(not (intro.title or "").strip() for intro in intros):
        raise app_error(422, "INTRO_TITLE_REQUIRED", "모든 인트로의 상황 제목을 작성해 주세요.")
    if any(len(intro.title) > INTRO_TITLE_LIMIT for intro in intros):
        raise app_error(
            422, "INTRO_TITLE_TOO_LONG", "인트로 상황 제목은 30자 이내로 작성해 주세요."
        )
    if any(not any((m.body or "").strip() for m in intro.messages) for intro in intros):
        raise app_error(422, "INTRO_BODY_REQUIRED", "모든 인트로에 대사나 서술을 작성해 주세요.")
    if any(sum(len(m.body or "") for m in intro.messages) > INTRO_BODY_LIMIT for intro in intros):
        raise app_error(
            422, "INTRO_TOO_LONG", "인트로 본문은 인트로마다 2000자 이내로 작성해 주세요."
        )
