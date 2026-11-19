"""2·3번 조각을 저장 상태와 함께 하나의 트랜잭션에 반영한다."""

import hashlib
import json
from collections import defaultdict

from app.features.creation.common.errors import app_error
from app.features.creation.common.names import has_surrounding_space, name_compare_key
from app.features.creation.intro import service as intro_service
from app.features.creation.save_visibility import repository
from app.features.creation.save_visibility.schemas import CreationSaveResponse, FieldIssue
from app.features.creation.speech_style import fewshot_service
from app.features.creation.speech_style import service as speech_style

CUSTOM_STYLE_LIMIT = 1000
FEWSHOTS_LIMIT = 2000
BASIC_TEXT_LIMIT = 3000
MIN_CHARACTERS = 1
MAX_CHARACTERS = 10
MAX_TAGS = 10
TAG_LIMIT = 10
BASIC_FIELDS = ("title", "subtitle", "world_description")
USER_PLACEHOLDER = "{{user}}"


def response(
    connection,
    story,
    groups=None,
    people=None,
    request_id=None,
    intros=None,
):
    few = (
        fewshot_service.read_snapshot(connection, story)
        if groups is None
        else fewshot_service.snapshot(story, groups, people, request_id)
    )
    return CreationSaveResponse(
        **few.model_dump(),
        expression=speech_style.read_expression_fields(connection, story["id"]),
        intros=intro_service.read_intros(connection, story["id"]) if intros is None else intros,
    )


def get_creation(connection, story_id, owner_id):
    with connection.transaction():
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        story = speech_style.find_owned_story(connection, story_id, owner_id)
        return response(connection, story)


def too_long(field, code, length, limit, message):
    over = length - limit
    return FieldIssue(
        field=field, code=code, value=str(over), message=f"{message} ({over:,}자 초과)"
    )


def _blank(value):
    return not value or not value.strip()


def collect_completed_issues(connection, story_id, body) -> list[FieldIssue]:
    """공개·비공개 저장 조건을 모두 모은다. Unicode 코드 포인트 단위, 원문을 바꾸지 않는다."""
    issues: list[FieldIssue] = []
    custom = body.expression.custom_style or ""
    if body.expression.style_mode == "custom" and len(custom) > CUSTOM_STYLE_LIMIT:
        issues.append(
            too_long(
                "expression.custom_style",
                "CUSTOM_STYLE_TOO_LONG",
                len(custom),
                CUSTOM_STYLE_LIMIT,
                "커스텀 문체는 1000자 이내로 작성해 주세요.",
            )
        )
    few_total = sum(len(m.body or "") for g in body.fewshots for m in g.messages)
    if few_total > FEWSHOTS_LIMIT:
        issues.append(
            too_long(
                "fewshots",
                "FEWSHOTS_TOO_LONG",
                few_total,
                FEWSHOTS_LIMIT,
                "퓨샷 전체를 2000자 이내로 작성해 주세요.",
            )
        )
    issues += intro_service.collect_completed_issues(body.intros)

    # TODO(KAN-13): common/ 조회 함수로 교체 (리뷰 server#5) — 1번 기본 정보·캐릭터
    basic = repository.read_basic(connection, story_id)
    people = repository.read_characters(connection, story_id)
    incomplete = "1번의 제목·한 줄 소개·세계관과 캐릭터 1~10명의 이름·상세를 먼저 작성해 주세요."
    for field in BASIC_FIELDS:
        if _blank(basic[field]):
            issues.append(
                FieldIssue(field=field, code="BASIC_SETTINGS_INCOMPLETE", message=incomplete)
            )
    if not MIN_CHARACTERS <= len(people) <= MAX_CHARACTERS:
        issues.append(
            FieldIssue(
                field="characters",
                code="BASIC_SETTINGS_INCOMPLETE",
                value=str(len(people)),
                message=incomplete,
            )
        )
    for index, person in enumerate(people):
        for field in ("name", "description"):
            if _blank(person[field]):
                issues.append(
                    FieldIssue(
                        field=f"characters[{index}].{field}",
                        code="BASIC_SETTINGS_INCOMPLETE",
                        message=incomplete,
                    )
                )
    text = [basic[field] or "" for field in BASIC_FIELDS] + [
        person[field] or "" for person in people for field in ("name", "description")
    ]
    basic_total = sum(len(value) for value in text)
    if basic_total > BASIC_TEXT_LIMIT:
        issues.append(
            too_long(
                "basic_text",
                "BASIC_SETTINGS_TOO_LONG",
                basic_total,
                BASIC_TEXT_LIMIT,
                "1번 기본 설정 전체가 3000자를 초과했습니다.",
            )
        )

    # TODO(KAN-13): common/ 조회 함수로 교체 (리뷰 server#5) — 5번 유저 프리셋 이름
    presets = repository.read_preset_names(connection, story_id)
    # 캐릭터의 빈 이름은 위 기본 설정 검사에서 이미 알린다.
    named = [(f"characters[{i}].name", p["name"]) for i, p in enumerate(people)]
    for index, name in enumerate(presets):
        field = f"user_presets[{index}].name"
        if _blank(name):
            # schema.sql: 완성 저장에서는 제공한 각 프리셋의 이름이 필수다.
            issues.append(
                FieldIssue(
                    field=field,
                    code="USER_PRESET_NAME_REQUIRED",
                    message="유저 프리셋의 이름을 입력해 주세요.",
                )
            )
        elif name != USER_PLACEHOLDER:
            # schema.sql: 정확한 {{user}} 표기는 여러 프리셋에 쓸 수 있어 중복 비교에서 뺀다.
            named.append((field, name))
    by_key = defaultdict(list)
    for field, name in named:
        if _blank(name):
            continue
        if has_surrounding_space(name):
            issues.append(
                FieldIssue(
                    field=field,
                    code="INVALID_CHARACTER_NAME",
                    message="이름 앞뒤에는 띄어쓰기를 쓸 수 없습니다.",
                )
            )
        else:
            by_key[name_compare_key(name)].append(field)
    for fields in by_key.values():
        if len(fields) > 1:
            issues += [
                FieldIssue(
                    field=field,
                    code="INVALID_CHARACTER_NAME",
                    message="같은 이름이 있습니다. 영문 대소문자와 띄어쓰기는 구분하지 않습니다.",
                )
                for field in fields
            ]

    # TODO(KAN-13): common/ 조회 함수로 교체 (리뷰 server#5) — 1번 해시태그
    tags = repository.read_tag_names(connection, story_id)
    tag_message = "해시태그는 최대10개, 각10자 이내로 작성해 주세요."
    if len(tags) > MAX_TAGS:
        issues.append(
            FieldIssue(field="tags", code="INVALID_TAGS", value=str(len(tags)), message=tag_message)
        )
    for index, tag in enumerate(tags):
        if not tag.strip() or len(tag) > TAG_LIMIT:
            issues.append(
                FieldIssue(
                    field=f"tags[{index}]", code="INVALID_TAGS", value=tag, message=tag_message
                )
            )
    return issues


def validate_completed(connection, story_id, body):
    """완성 저장 거절. `code`·`detail`은 첫 문제, `issues`에는 필드별 문제를 모두 담는다."""
    issues = collect_completed_issues(connection, story_id, body)
    if issues:
        raise app_error(
            422,
            issues[0].code,
            issues[0].message,
            issues=[issue.model_dump() for issue in issues],
        )


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
        speech_style.lock_write_request(connection, owner_id, request_id)
        story = speech_style.find_owned_story(connection, story_id, owner_id, lock=True)
        previous = speech_style.find_write_request(connection, owner_id, request_id)
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
        speech_style.write_expression_fields(connection, story_id, body.expression)
        story.update(repository.update_save_state(connection, story_id, target))
        result = response(connection, story, groups, people, request_id, intros)
        speech_style.remember_write_request(
            connection, owner_id, request_id, story_id, digest, result.model_dump(mode="json")
        )
        return result
