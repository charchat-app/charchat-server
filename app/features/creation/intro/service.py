"""CREATE-US-03 인트로 조회·컬렉션 저장·참조 검증·완성 저장 조건."""

from app.features.creation.common.errors import app_error
from app.features.creation.initial_state import service as initial_state
from app.features.creation.intro import repository as repo
from app.features.creation.intro.schemas import INTRO_BODY_LIMIT, INTRO_TITLE_LIMIT
from app.features.creation.save_visibility.schemas import FieldIssue


def read_intros(connection, story_id):
    # CREATE-US-04 인트로별 장면을 함께 돌려준다. 행이 없으면 미지정 장면.
    scenes = initial_state.read_scenes(connection, story_id)
    grouped = {}
    for row in repo.read_messages(connection, story_id):
        grouped.setdefault(row["intro_id"], []).append(
            {
                "id": row["id"],
                "client_key": f"intro-message-{row['id']}",
                "speaker_kind": row["speaker_kind"],
                "character_id": row["character_id"],
                "body": row["body"],
            }
        )
    return [
        {
            "id": intro["id"],
            "client_key": f"intro-{intro['id']}",
            "title": intro["title"],
            "messages": grouped.get(intro["id"], []),
            "scene": scenes.get(intro["id"]) or initial_state.empty_scene(),
        }
        for intro in repo.read_intros(connection, story_id)
    ]


def write_intros(connection, story_id, intros, character_ids):
    old_intros = {i["id"] for i in repo.read_intros(connection, story_id)}
    old_messages = {m["id"]: m["intro_id"] for m in repo.read_messages(connection, story_id)}
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
    initial_state.check_scene_people(intros, character_ids)
    result = repo.write_collection(connection, story_id, intros, old_intros, set(old_messages))
    for intro, saved in zip(intros, result, strict=True):
        saved["scene"] = initial_state.write_scene(connection, saved["id"], intro.scene)
    return result


def collect_completed_issues(intros) -> list[FieldIssue]:
    """공개·비공개 저장 조건(CREATE-US-03-AC03). Unicode 코드 포인트 단위, 원문을 바꾸지 않는다.

    인트로마다 필드별 문제를 모두 돌려준다. 길이 초과의 `value`는 초과 글자 수다.
    """
    if not intros:
        return [
            FieldIssue(
                field="intros", code="INTRO_REQUIRED", message="인트로를 1개 이상 작성해 주세요."
            )
        ]
    issues: list[FieldIssue] = []
    for index, intro in enumerate(intros):
        path = f"intros[{index}]"
        title = intro.title or ""
        if not title.strip():
            issues.append(
                FieldIssue(
                    field=f"{path}.title",
                    code="INTRO_TITLE_REQUIRED",
                    message="인트로의 상황 제목을 작성해 주세요.",
                )
            )
        elif len(title) > INTRO_TITLE_LIMIT:
            over = len(title) - INTRO_TITLE_LIMIT
            issues.append(
                FieldIssue(
                    field=f"{path}.title",
                    code="INTRO_TITLE_TOO_LONG",
                    value=str(over),
                    message=(
                        f"인트로 상황 제목은 {INTRO_TITLE_LIMIT}자 이내로 작성해 주세요. "
                        f"({over:,}자 초과)"
                    ),
                )
            )
        if not any((m.body or "").strip() for m in intro.messages):
            issues.append(
                FieldIssue(
                    field=f"{path}.messages",
                    code="INTRO_BODY_REQUIRED",
                    message="인트로에 대사나 서술을 작성해 주세요.",
                )
            )
        else:
            total = sum(len(m.body or "") for m in intro.messages)
            if total > INTRO_BODY_LIMIT:
                over = total - INTRO_BODY_LIMIT
                issues.append(
                    FieldIssue(
                        field=f"{path}.messages",
                        code="INTRO_TOO_LONG",
                        value=str(over),
                        message=(
                            f"인트로 본문이 {total:,}자예요. 인트로마다 "
                            f"{INTRO_BODY_LIMIT:,}자 이내로 줄여 주세요. ({over:,}자 초과)"
                        ),
                    )
                )
    return issues
