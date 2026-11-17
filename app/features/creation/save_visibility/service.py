"""2·3·4번 조각을 저장 상태와 함께 하나의 트랜잭션에 반영한다."""

import hashlib
import json

from app.features.creation.common.errors import app_error
from app.features.creation.initial_state import service as initial_state
from app.features.creation.intro import service as intro_service
from app.features.creation.save_visibility.schemas import CreationSaveResponse
from app.features.creation.speech_style import fewshot_service, repository
from app.features.creation.speech_style.schemas import DEFAULT_EXPRESSION, ExpressionFields
from app.features.creation.speech_style.service import require_story


def response(
    connection,
    story,
    groups=None,
    people=None,
    request_id=None,
    intros=None,
    relations=None,
    affinities=None,
):
    few = (
        fewshot_service.read_snapshot(connection, story)
        if groups is None
        else fewshot_service.snapshot(story, groups, people, request_id)
    )
    fields = repository.read_expression(connection, story["id"])
    return CreationSaveResponse(
        **few.model_dump(),
        expression=ExpressionFields(**fields) if fields else DEFAULT_EXPRESSION,
        intros=intro_service.read_intros(connection, story["id"]) if intros is None else intros,
        relations=initial_state.read_relations(connection, story["id"])
        if relations is None
        else relations,
        affinities=initial_state.read_affinities(connection, story["id"])
        if affinities is None
        else affinities,
    )


def get_creation(connection, story_id, owner_id):
    with connection.transaction():
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        story = repository.find_story(connection, story_id, owner_id)
        require_story(story)
        return response(connection, story)


def validate_completed(connection, story_id, body):
    # Unicode 코드 포인트 단위. 원문을 정규화·치환·잘라내지 않는다.
    if body.expression.style_mode == "custom" and len(body.expression.custom_style or "") > 1000:
        raise app_error(422, "CUSTOM_STYLE_TOO_LONG", "커스텀 문체는 1000자 이내로 작성해 주세요.")
    if sum(len(m.body or "") for g in body.fewshots for m in g.messages) > 2000:
        raise app_error(422, "FEWSHOTS_TOO_LONG", "퓨샷 전체를 2000자 이내로 작성해 주세요.")
    intro_service.validate_completed(body.intros)
    initial_state.validate_completed(body.intros, body.relations, body.affinities)
    basic = connection.execute(
        "SELECT title,subtitle,world_description FROM stories WHERE id=%s", (story_id,)
    ).fetchone()
    people = connection.execute(
        "SELECT name,description FROM characters WHERE story_id=%s ORDER BY position,id",
        (story_id,),
    ).fetchall()
    text = list(basic.values()) + [value for person in people for value in person.values()]
    if not 1 <= len(people) <= 10 or any(not value or not value.strip() for value in text):
        raise app_error(
            422,
            "BASIC_SETTINGS_INCOMPLETE",
            "1번의 제목·한 줄 소개·세계관과 캐릭터 1~10명의 이름·상세를 먼저 작성해 주세요.",
        )
    if sum(len(value) for value in text) > 3000:
        raise app_error(
            422, "BASIC_SETTINGS_TOO_LONG", "1번 기본 설정 전체가 3000자를 초과했습니다."
        )
    presets = connection.execute(
        "SELECT name FROM story_user_presets WHERE story_id=%s", (story_id,)
    ).fetchall()
    names = [person["name"] for person in people] + [p["name"] for p in presets if p["name"]]
    normalized = ["".join(name.split()).lower() for name in names]
    if any(name != name.strip() for name in names) or len(set(normalized)) != len(normalized):
        raise app_error(
            422, "INVALID_CHARACTER_NAME", "캐릭터 이름의 앞뒤 공백과 중복을 확인해 주세요."
        )
    tags = connection.execute(
        "SELECT t.name FROM tags t JOIN story_tags s ON s.tag_id=t.id WHERE s.story_id=%s",
        (story_id,),
    ).fetchall()
    if len(tags) > 10 or any(not tag["name"].strip() or len(tag["name"]) > 10 for tag in tags):
        raise app_error(422, "INVALID_TAGS", "해시태그는 최대10개, 각10자 이내로 작성해 주세요.")


def save_creation(connection, story_id, owner_id, request_id, body):
    payload = {
        "operation": "creation-save-v3",
        "story_id": str(story_id),
        **body.model_dump(mode="json"),
    }
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
            return CreationSaveResponse.model_validate(previous["response"])
        current, target = story["save_state"], body.save_state
        if (current != "draft" and target == "draft") or (
            current == "public" and target == "private"
        ):
            raise app_error(
                409,
                "SAVE_STATE_TRANSITION",
                "완성 저장은 임시 저장으로, 공개는 비공개로 되돌릴 수 없습니다.",
            )
        if body.mode == "auto" and (current != "draft" or target != "draft"):
            raise app_error(409, "AUTO_SAVE_NOT_ALLOWED", "자동 저장은 임시 저장에만 적용됩니다.")
        if target != "draft":
            validate_completed(connection, story_id, body)
        groups, people = fewshot_service.write_fewshots(connection, story_id, body)
        character_ids = {p["id"] for p in people}
        intros = intro_service.write_intros(connection, story_id, body.intros, character_ids)
        relations = initial_state.write_relations(
            connection, story_id, body.relations, character_ids
        )
        affinities = initial_state.write_affinities(
            connection, story_id, body.affinities, character_ids
        )
        repository.write_expression(connection, story_id, body.expression.model_dump(mode="json"))
        story.update(
            connection.execute(
                "UPDATE stories SET save_state=%s WHERE id=%s RETURNING save_state,saved_at",
                (target, story_id),
            ).fetchone()
        )
        result = response(
            connection, story, groups, people, request_id, intros, relations, affinities
        )
        repository.remember_request(
            connection, owner_id, request_id, story_id, digest, result.model_dump(mode="json")
        )
        return result
