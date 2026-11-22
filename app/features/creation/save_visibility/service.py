"""2·3·4·5번 조각을 저장 상태와 함께 하나의 트랜잭션에 반영한다."""

import hashlib
import json
from collections import defaultdict
from contextlib import ExitStack

from pydantic_core import to_jsonable_python

from app.features.creation.common.errors import app_error
from app.features.creation.common.names import has_surrounding_space, name_compare_key
from app.features.creation.images import service as images_service
from app.features.creation.initial_state import service as initial_state
from app.features.creation.intro import service as intro_service
from app.features.creation.save_visibility import rating_review, repository, working_copy
from app.features.creation.save_visibility.schemas import (
    CreationSaveRequest,
    CreationSaveResponse,
    FieldIssue,
    PresetResponse,
    StagedCharacterImagesResponse,
)
from app.features.creation.speech_style import fewshot_service
from app.features.creation.speech_style import service as speech_style
from app.features.creation.story_basic import service as story_basic
from app.features.creation.story_basic.schemas import CharacterWriteItem, StoryUpdateRequest
from app.features.creation.user_profile import service as preset_service
from app.features.creation.user_profile.schemas import AuthorizedStory, PresetDraft

CUSTOM_STYLE_LIMIT = 1000
FEWSHOTS_LIMIT = 2000
BASIC_TEXT_LIMIT = 3000
MIN_CHARACTERS = 1
MAX_CHARACTERS = 10
MAX_TAGS = 10
TAG_LIMIT = 10
BASIC_FIELDS = ("title", "subtitle", "world_description")
SCENE_FIELDS = ("fewshots", "expression", "intros", "relations", "affinities")


def with_image_urls(connection, owner_id, presets: list[PresetResponse]) -> list[PresetResponse]:
    """프리셋 응답에 읽기 전용 미리보기 URL을 채운다(CREATE-US-05 미리보기 복원).

    소유자 이미지만 잠그지 않고 조회한다. 남의·없는 이미지는 URL을 비운다.
    """
    urls = images_service.owned_image_urls(connection, owner_id, [p.image_id for p in presets])
    return [p.model_copy(update={"image_url": urls.get(p.image_id)}) for p in presets]


def canonical_presets(connection, story_ref: AuthorizedStory) -> list[PresetResponse]:
    """이용자가 보는 저장본 프리셋(편집 순서)."""
    presets = [
        PresetResponse(**record.to_draft().model_dump())
        for record in preset_service.read_presets(connection, story_ref)
    ]
    return with_image_urls(connection, story_ref.owner_id, presets)


def rating_fields(connection, story_id, staged_rating=None) -> dict:
    """CREATE-US-08 응답 등급 필드. 선택은 임시 저장한 값이 있으면 그것, 없으면 저장본이다.

    적용 등급·검토 상태는 언제나 저장본 값이다.
    """
    row = repository.read_rating(connection, story_id)
    return {
        "requested_rating": staged_rating or row["requested_rating"],
        "effective_rating": row["effective_rating"],
        "rating_review_status": row["rating_review_status"],
    }


def response(
    connection,
    story,
    owner_id,
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
    return CreationSaveResponse(
        **few.model_dump(),
        **rating_fields(connection, story["id"]),
        expression=speech_style.read_expression_fields(connection, story["id"]),
        intros=intro_service.read_intros(connection, story["id"]) if intros is None else intros,
        relations=initial_state.read_relations(connection, story["id"])
        if relations is None
        else relations,
        affinities=initial_state.read_affinities(connection, story["id"])
        if affinities is None
        else affinities,
        presets=canonical_presets(connection, AuthorizedStory(story["id"], owner_id)),
    )


def working_response(connection, story, owner_id, document, saved_at, request_id=None):
    """임시 저장본으로 만든 응답. 스토리 정보·캐릭터 목록은 현재 값을 쓴다.

    프리셋은 임시 저장한 원문이 있으면 그것을, 없으면 저장본을 보여 준다(`presets_pending`).
    """
    base = fewshot_service.snapshot(
        story, document["fewshots"], fewshot_service.characters(connection, story["id"]), request_id
    )
    staged = working_copy.staged_presets(document)
    presets = (
        canonical_presets(connection, AuthorizedStory(story["id"], owner_id))
        if staged is None
        else with_image_urls(connection, owner_id, [PresetResponse(**item) for item in staged])
    )
    return CreationSaveResponse(
        **base.model_dump(),
        **rating_fields(connection, story["id"], working_copy.staged_requested_rating(document)),
        expression=document["expression"],
        intros=document["intros"],
        relations=document["relations"],
        affinities=document["affinities"],
        working_copy_saved_at=saved_at,
        presets=presets,
        presets_pending=staged is not None,
        basic=document.get("basic"),
        staged_character_images=staged_faces_response(connection, owner_id, document),
    )


def staged_faces_response(connection, owner_id, document) -> list[StagedCharacterImagesResponse]:
    """CREATE-US-07-AC09: 임시 저장한 새 캐릭터 사진을 사진 API와 같은 항목으로 돌려준다."""
    faces = working_copy.staged_character_images(document)
    if not faces:
        return []
    items = images_service.owned_image_items(
        connection, owner_id, working_copy.staged_character_image_ids(document)
    )
    return [
        StagedCharacterImagesResponse(
            basic_index=entry["basic_index"],
            images=[items[i] for i in entry["image_ids"] if i in items],
        )
        for entry in faces
    ]


def face_entries(body) -> list:
    """`images.characters` 중 새 캐릭터(`basic_index`)를 가리키는 항목."""
    if body.images is None:
        return []
    return [entry for entry in body.images.characters if entry.basic_index is not None]


def existing_character_images(images):
    """`images`에서 기존 캐릭터·표지만 남긴다. 남는 것이 없으면 None(사진 임시 저장본 그대로)."""
    if images is None:
        return None
    people = [entry for entry in images.characters if entry.character_id is not None]
    if not people and images.story_image_ids is None:
        return None
    return images.model_copy(update={"characters": people})


def staged_face_lists(connection, story_id, staged_raw, faces, planned, before_ids):
    """CREATE-US-07-AC09: 임시 저장한 새 캐릭터 사진을 방금 만든 캐릭터 ID에 짝짓는다.

    사진은 임시 저장한 `basic`의 위치(`basic_index`)로 기록돼 있다. 명단 정리(plan_characters)는
    기존 캐릭터만 빼거나 뒤에 붙이고 새 행(id null)의 상대 순서는 바꾸지 않으므로, 임시 목록에서
    몇 번째 새 행인지로 반영 뒤 목록의 새 행을 찾는다. 짝을 하나로 정할 수 없으면(기록이
    맞지 않거나 반영 결과가 계획과 다르면) 추측하지 않고 409로 거절한다.
    """
    staged_new = [
        i for i, c in enumerate((staged_raw or {}).get("characters") or []) if c.get("id") is None
    ]
    planned_rows = planned.characters or []
    planned_new = [i for i, c in enumerate(planned_rows) if c.id is None]
    people = [person["id"] for person in repository.read_characters(connection, story_id)]
    conflict = _character_issue(
        409,
        "BASIC_CHARACTER_ROSTER_CHANGED",
        "임시 저장 뒤 캐릭터 목록이 바뀌었습니다. 기본 정보를 다시 확인해 주세요.",
    )
    if len(staged_new) != len(planned_new) or len(people) != len(planned_rows):
        raise conflict
    lists: dict[int, list[int]] = {}
    for entry in faces:
        if entry["basic_index"] not in staged_new:
            raise conflict
        target = people[planned_new[staged_new.index(entry["basic_index"])]]
        if target in before_ids:
            raise conflict
        lists[target] = entry["image_ids"]
    return lists


BASIC_BASELINE = "basic_baseline"


def staged_basic(copy):
    """임시 저장본에 둔 1번 기본 정보(원문 JSON). 없으면 None."""
    creation = copy["creation"] if copy else None
    return creation.get("basic") if creation else None


def staged_baseline(copy):
    """기본 정보를 임시 저장할 때 기록한 저장본 캐릭터 ID 명단. 없으면(이전 임시 저장본) None.

    응답에는 싣지 않는 내부 메타데이터다. 「저장」에서 캐릭터 삭제를 반영할 때만 쓴다.
    """
    creation = copy["creation"] if copy else None
    baseline = creation.get(BASIC_BASELINE) if creation else None
    return set(baseline["character_ids"]) if baseline else None


def apply_basic_with_existing_roster(connection, story, basic):
    """다른 편집 영역의 연결을 지우지 않도록 공통 저장에서 명단을 유지한다."""
    story_id = story["id"]
    if basic.characters is not None:
        requested = [character.id for character in basic.characters]
        existing = {person["id"] for person in fewshot_service.characters(connection, story_id)}
        if len(requested) != len(existing) or set(requested) != existing:
            issue = FieldIssue(
                field="characters",
                code="BASIC_CHARACTER_MEMBERSHIP_UNSUPPORTED",
                message="캐릭터 추가·삭제는 현재 공통 저장에서 반영할 수 없습니다.",
            )
            raise app_error(422, issue.code, issue.message, issues=[issue.model_dump()])
    story["title"] = story_basic.apply_basic(connection, story_id, basic)["title"]


AUTOSAVE_FINALIZE = "autosave_finalize"


def autosave_unchanged(connection, story, owner_id, copy, auto_request_id) -> bool:
    """CREATE-US-07-AC09: 그 자동 저장 뒤로 저장본·임시 저장본이 그대로인지.

    스토리 잠금 안에서 부른다. 다음을 모두 만족할 때만 True다.
    - 기록한 응답이 확정 가능(자기 요청키)하고 같은 스토리의 것이다.
    - 응답의 저장본 시각(saved_at)·2~5번·6번 임시 저장 시각이 지금과 같다.
    - 지금 임시 저장본을 마지막으로 쓴 것이 그 자동 저장이다.
    사진만 바꾼 자동 저장(`PUT /images` mode=auto) 기록이면 사진 쪽 확인으로 넘긴다.
    """
    previous = speech_style.find_write_request(connection, owner_id, auto_request_id)
    if not previous or previous["response"].get("finalize_auto_request_id") != auto_request_id:
        return False
    if images_service.is_images_response(previous["response"]):
        return images_service.photo_autosave_matches(
            story, copy, previous["response"], auto_request_id
        )
    recorded = CreationSaveResponse.model_validate(previous["response"])
    creation = copy["creation"] if copy else None
    marker = (creation or {}).get(AUTOSAVE_FINALIZE) or {}
    return (
        recorded.story_id == story["id"]
        and marker.get("request_id") == auto_request_id
        and recorded.saved_at == story["saved_at"]
        and recorded.working_copy_saved_at == copy["creation_saved_at"]
        and recorded.images_working_copy_saved_at
        == images_service.images_working_copy_saved_at(connection, story["id"])
    )


def autosave_finalizable(connection, story, owner_id, copy) -> bool:
    """이번 공통 자동 저장을 확정할 수 있는지(쓰기 전 임시 저장본 기준).

    앞에 임시 저장본이 없었거나, 앞의 임시 저장본이 아직 바뀌지 않은
    확정 가능 자동 저장일 때다.
    """
    if not copy or (copy["creation"] is None and copy["images"] is None):
        return True
    marker = (copy["creation"] or {}).get(AUTOSAVE_FINALIZE)
    if not marker:
        return False
    return autosave_unchanged(connection, story, owner_id, copy, marker["request_id"])


def get_creation(connection, story_id, owner_id):
    """편집 화면용 조회. 임시 저장본이 있으면 그것을, 없으면 저장본을 돌려준다."""
    with connection.transaction():
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        story = speech_style.find_owned_story(connection, story_id, owner_id)
        copy = working_copy.read(connection, story_id)
        if copy and copy["creation"] is not None:
            result = working_response(
                connection, story, owner_id, copy["creation"], copy["creation_saved_at"]
            )
        else:
            result = response(connection, story, owner_id)
        result.images_working_copy_saved_at = images_service.images_working_copy_saved_at(
            connection, story_id
        )
        mark_rating_only(result, copy)
        return result


def mark_rating_only(result, copy) -> None:
    """CREATE-US-08-AC13: 등급만 담은 임시 저장본이고 사진 임시 저장본이 없을 때만 true."""
    result.rating_only_working_copy = (
        working_copy.is_rating_only((copy or {}).get("creation"))
        and result.images_working_copy_saved_at is None
    )


def rating_copy_unchanged(story, body, copy) -> bool:
    """CREATE-US-08-AC13: 스토리 잠금 안에서, 반영하려는 등급 임시 저장본이 그대로인지."""
    return bool(
        copy
        and copy["images"] is None
        and working_copy.is_rating_only(copy["creation"])
        and copy["creation_saved_at"] == body.finalize_rating_working_copy_at
        and body.save_state == story["save_state"]
    )


def too_long(field, code, length, limit, message):
    over = length - limit
    return FieldIssue(
        field=field, code=code, value=str(over), message=f"{message} ({over:,}자 초과)"
    )


def _blank(value):
    return not value or not value.strip()


def collect_completed_issues(connection, story_ref: AuthorizedStory, body) -> list[FieldIssue]:
    """공개·비공개 저장 조건을 모두 모은다. Unicode 코드 포인트 단위, 원문을 바꾸지 않는다.

    5번 프리셋·6번 이미지는 이번 저장에서 반영한 뒤의 값(같은 트랜잭션)을 검사한다.
    """
    story_id = story_ref.story_id
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
    issues += initial_state.collect_completed_issues(body.intros, body.relations, body.affinities)

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
    # 6번 이미지(CREATE-US-06-AC08): 등장 캐릭터마다 이미지 1장 이상
    issues += [
        FieldIssue(**issue) for issue in images_service.collect_image_issues(connection, story_id)
    ]

    # 5번 유저 프리셋(CREATE-US-05): 요청에서 뺐어도 반영 뒤 저장본 프리셋 전체를 프리셋 모듈
    # 규칙(0~5개, 이름·이미지 필수, 이름+상세 1000자, 앞뒤 띄어쓰기, 캐릭터·다른 직접 이름과
    # 중복, `{{user}}` 여러 개 허용)으로 검사한다.
    presets = preset_service.read_presets(connection, story_ref)
    issues += [
        FieldIssue(**issue.model_dump())
        for issue in preset_service.collect_preset_issues(
            [record.to_draft() for record in presets], [person["name"] for person in people]
        )
    ]
    issues += [
        FieldIssue(
            field=f"presets[{index}].image_id",
            code="INVALID_PRESET_IMAGE",
            message="사용할 수 없는 이미지입니다.",
        )
        for index in preset_service.invalid_image_fields(connection, story_ref, presets)
    ]

    # 캐릭터끼리의 이름 규칙. 캐릭터의 빈 이름은 위 기본 설정 검사에서 이미 알린다.
    named = [(f"characters[{i}].name", p["name"]) for i, p in enumerate(people)]
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
    basic = body.basic
    basic_tags = basic.tags if basic is not None else None
    if basic_tags is not None:
        issues += basic_tag_issues(basic_tags)
        return issues
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


def basic_tag_issues(raw_tags: list[str]) -> list[FieldIssue]:
    """기본 정보 API와 같은 태그를 검사하고 오류 위치는 제출 원문에 맞춘다."""
    by_tag: dict[str, int] = {}
    for index, raw in enumerate(raw_tags):
        tag = story_basic.strip_tag_mark(raw)
        if tag:
            by_tag.setdefault(tag, index)

    source_indices = list(by_tag.values())
    issues = []
    for issue in story_basic.collect_tag_issues(list(by_tag)):
        detail = issue.model_dump()
        if issue.field.startswith("tags["):
            source_index = source_indices[int(issue.field[5:-1])]
            detail.update(field=f"tags[{source_index}]", value=raw_tags[source_index])
        issues.append(FieldIssue(**detail))
    return issues


def validate_completed(connection, story_ref: AuthorizedStory, body):
    """완성 저장 거절. `code`·`detail`은 첫 문제, `issues`에는 필드별 문제를 모두 담는다."""
    issues = collect_completed_issues(connection, story_ref, body)
    if issues:
        raise app_error(
            422,
            issues[0].code,
            issues[0].message,
            issues=[issue.model_dump() for issue in issues],
        )


def save_creation(
    connection, story_id, owner_id, request_id, body, storage=None, review_coordinator=None
):
    """2~4번 공통 저장. 「저장」이면 이미지 임시 저장본도 함께 검사·반영한다(안 B).

    반영으로 연결이 끊긴 예전 사진은 커밋 뒤 바로 지운다(다른 곳에서 쓰는 사진은 남는다).
    CREATE-US-08-AC02: 조정기를 주면 비공개·공개 「저장」이 커밋된 뒤 그 저장본을 조정기에
    알린다(UNSAFE면 검토를 넘기고, 아니면 앞선 검토를 무효로 한다). `after_commit`은
    트랜잭션을 감싸므로 그 안의 울타리는 COMMIT이 끝난 뒤에 나온다.
    """
    with ExitStack() as after_commit:
        result, released = _save_creation(
            connection, story_id, owner_id, request_id, body, review_coordinator, after_commit
        )
    if storage is not None and released:
        images_service.release_images(connection, storage, released)
    return result


def _character_issue(status, code, message):
    issue = FieldIssue(field="characters", code=code, message=message)
    return app_error(status, issue.code, issue.message, issues=[issue.model_dump()])


def restaged_baseline(connection, story_id, basic, previous_creation):
    current = {person["id"] for person in repository.read_characters(connection, story_id)}
    previous_copy = {"creation": previous_creation} if previous_creation else None
    previous_basic = staged_basic(previous_copy)
    if previous_basic is None:
        return current
    # 재임시저장은 새로 추가된 인물의 누락을 삭제 의도로 승격하지 않는다. 이전 명단은 이전
    # 기본 정보의 캐릭터 목록이 비어 있어도(null) 그대로 유지한다.
    explicitly_seen = {character.id for character in (basic.characters or [])} & current
    baseline = staged_baseline(previous_copy)
    if baseline is None:
        # 이전 목록에 있던 ID만 알아본 것으로 처리한다. 이미 지워진 ID는 다시 만들지 않는다.
        baseline = {
            person["id"]
            for person in previous_basic.get("characters") or []
            if person.get("id") is not None
        }
    return baseline | explicitly_seen


def plan_characters(connection, story_id, basic, staged, baseline, allow_new=False):
    """「저장」에 반영할 기본 정보와 지울 기존 캐릭터 ID를 정한다. (기본 정보, 삭제 ID 집합)

    기존 캐릭터만 남기거나 빼는 변경만 반영한다. 새 캐릭터(id 없음)·이 스토리에 없는 id·
    같은 id 반복은 새 ID의 사진 연결이 필요하거나 목록이 올바르지 않아 거절한다.
    예외(`allow_new`): 직접 보낸 기본 정보에 `images`가 함께 오면 새 캐릭터를 허용한다.
    새 캐릭터마다 `basic_index` 이미지 목록이 있는지는 요청 검증이 확인했다(CREATE-US-07-AC07).
    임시 저장한 기본 정보의 새 캐릭터도 허용한다. 함께 임시 저장한 사진을 연결하고, 사진이 없는
    새 캐릭터는 완성 검사(CHARACTER_IMAGE_REQUIRED)가 거절한다(CREATE-US-07-AC09).

    삭제는 임시 저장한 기본 정보(`staged`)로만 반영한다. 임시 저장 당시의 저장본 명단
    (`baseline`)에 있었는데 목록에서 뺀 캐릭터만 직접 지운 것으로 본다. 그 뒤 다른 화면에서
    추가된 캐릭터는 오래된 목록에 없을 뿐(누락)이므로 지우지 않고, 그 편집자가 마지막으로
    저장한 값 그대로 목록 끝에 남긴다(CREATE-US-07-AC02: 마지막으로 성공한 저장 기준).
    명단이 없는 이전 임시 저장본은 삭제와 누락을 구분할 수 없어 빠진 캐릭터가 있으면 거절한다.

    임시 저장 뒤 다른 저장이 이미 지운 캐릭터(임시 저장 목록에는 남아 있음)는 그 저장이 더
    나중에 성공했으므로 되살리지 않고 목록에서 뺀 채 나머지를 반영한다(AC02). 같은 ID로
    다시 만들 수 없고, 빼는 것은 저장본 데이터를 지우지 않는다. 기준 명단이 있으면 명단에
    있던 ID만 이렇게 보고, 명단에 없던 ID는 잘못된 목록으로 거절한다. 명단이 없는 이전
    임시 저장본은 그 ID가 원래 있었는지 알 수 없지만 빼도 아무것도 지우지 않으므로 같게
    처리한다. 직접 보낸 기본 정보는 지금 목록과 정확히 맞아야 한다.
    """
    requested = basic.characters
    ids = [character.id for character in requested]
    current = repository.read_characters(connection, story_id)
    existing = {person["id"] for person in current}
    vanished = set(ids) - existing - {None}
    if staged and vanished and (baseline is None or vanished <= baseline):
        requested = [character for character in requested if character.id not in vanished]
        basic = basic.model_copy(update={"characters": requested})
        ids = [character.id for character in requested]
    known = [i for i in ids if i is not None]
    new_rejected = None in ids and not allow_new
    if new_rejected or len(known) != len(set(known)) or not set(known) <= existing:
        raise _character_issue(
            422,
            "BASIC_CHARACTER_MEMBERSHIP_UNSUPPORTED",
            "캐릭터 추가는 현재 공통 저장에서 반영할 수 없습니다.",
        )
    omitted = existing - set(known)
    if not omitted:
        return basic, set()
    if not staged:
        raise _character_issue(
            422,
            "BASIC_CHARACTER_MEMBERSHIP_UNSUPPORTED",
            "캐릭터 삭제는 임시 저장한 기본 정보로만 반영할 수 있습니다.",
        )
    if baseline is None:
        raise _character_issue(
            409,
            "BASIC_CHARACTER_ROSTER_CHANGED",
            "임시 저장 뒤 캐릭터 목록이 바뀌었습니다. 기본 정보를 다시 확인해 주세요.",
        )
    removed = omitted & baseline
    later = [
        CharacterWriteItem(id=p["id"], name=p["name"], description=p["description"])
        for p in current
        if p["id"] in omitted - baseline
    ]
    if later:
        basic = basic.model_copy(update={"characters": [*requested, *later]})
    return basic, removed


def requested_image_ids(body) -> set[int]:
    """`images`로 보낸 캐릭터 얼굴·스토리 표지 이미지 ID 전체(검사 전 원문)."""
    if body.images is None:
        return set()
    faces = {i for entry in body.images.characters for i in entry.image_ids}
    return faces | set(body.images.story_image_ids or ())


def explicit_photo_cohort(connection, story_id, body):
    if body.images is None:
        return set()
    people = [e.character_id for e in body.images.characters if e.character_id is not None]
    return images_service.explicit_image_cohort(connection, story_id, people) | requested_image_ids(
        body
    )


def apply_image_references(connection, story_id, owner_id, body, admitted) -> set[int]:
    """CREATE-US-07-AC07: `images` 참조를 서버 캐릭터 ID로 바꿔 같은 트랜잭션에 연결한다.

    1번 기본 정보·사진 임시 저장본을 반영한 뒤 부른다. 그래서 새 캐릭터는 이미 ID가 있고,
    보낸 캐릭터의 목록은 임시 저장본보다 우선하며 담지 않은 캐릭터는 임시 저장본 반영 결과를
    유지한다. `basic_index`는 직접 보낸 `basic.characters`의 위치다. 그 경우 목록이 지금
    캐릭터 전체와 정확히 같아야 반영되므로(plan_characters) 위치가 곧 position이다.
    `character_id`는 반영 뒤에도 이 스토리에 남은 캐릭터여야 한다(지운 캐릭터면 거절).
    `story_image_ids`(표지)도 같은 방식으로 사진 임시 저장본 반영 결과 위에 덮어쓰고,
    null이면 그 결과를 그대로 둔다(CREATE-US-07-AC07·AC08).
    커밋 뒤 지울 후보 이미지 ID를 돌려준다.
    """
    if body.images is None:
        return set()
    people = [person["id"] for person in repository.read_characters(connection, story_id)]
    roster = body.basic.characters if body.basic is not None else None
    lists: dict[int, list[int]] = {}
    for entry in body.images.characters:
        if entry.basic_index is not None:
            if roster is None or len(roster) != len(people):
                raise RuntimeError("basic_index 위치와 저장된 캐릭터 순서가 맞지 않습니다.")
            target = people[entry.basic_index]
        elif entry.character_id in people:
            target = entry.character_id
        else:
            raise _character_issue(
                422,
                "CHARACTER_NOT_FOUND",
                "이 스토리에 없는 캐릭터의 이미지입니다. 캐릭터 목록을 다시 확인해 주세요.",
            )
        lists[target] = entry.image_ids
    return images_service.apply_character_images(
        connection,
        owner_id,
        lists,
        set(admitted),
        story_id=story_id,
        story_image_ids=body.images.story_image_ids,
    )


def write_story_parts(connection, story_id, body, character_ids):
    """3·4번 인트로·관계·호감도를 `character_ids`의 인물만 허용해 저장한다."""
    intros = intro_service.write_intros(connection, story_id, body.intros, character_ids)
    relations = initial_state.write_relations(connection, story_id, body.relations, character_ids)
    affinities = initial_state.write_affinities(
        connection, story_id, body.affinities, character_ids
    )
    return intros, relations, affinities


def request_digest(story_id, body) -> str:
    """요청키 비교용 해시.

    프리셋을 빼거나 null로 보낸 요청은 그 필드를 빼고 해시해, 프리셋 연결 전과 같은 해시를 쓴다.
    그래서 연결 전에 기록된 요청키 재전송도 기록한 응답을 그대로 돌려받는다.
    """
    exclude = {"presets"} if body.presets is None else set()
    if body.basic is None:
        exclude.add("basic")
    if body.images is None:
        exclude.add("images")
    if body.finalize_auto_request_id is None:
        exclude.add("finalize_auto_request_id")
    if body.requested_rating is None:
        # CREATE-US-08-AC13: 등급을 뺀 요청은 등급 연결 전과 같은 해시를 쓴다.
        exclude.add("requested_rating")
    if body.finalize_rating_working_copy_at is None:
        exclude.add("finalize_rating_working_copy_at")
    exclude.update(SCENE_FIELDS if body.preserve_scene else ("preserve_scene",))
    payload = {
        "operation": "creation-save-v4",
        "story_id": str(story_id),
        **body.model_dump(mode="json", exclude=exclude),
    }
    if body.images is not None and body.images.story_image_ids is None:
        payload["images"].pop("story_image_ids", None)
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def preset_image_cohort(connection, story_ref: AuthorizedStory, chosen, copy) -> set[int]:
    """이 저장이 연결을 바꿀 수 있는 프리셋 이미지 ID 전체(잠금 묶음).

    - 저장본 프리셋 이미지: `replace_presets`가 끊을 수 있는 예전 ID
    - 이번에 쓸 프리셋(`chosen`, 보낸 값 또는 임시 저장한 값) 이미지: 새로 연결할 ID
    - 임시 저장본 프리셋 이미지: 임시 저장본을 쓰거나 비울 때 보호 연결이 바뀌는 ID
    원문이라 남의·없는 ID가 섞일 수 있다. 소유 확인은 잠금·`replace_presets`가 한다.
    """
    old = {record.image_id for record in preset_service.read_presets(connection, story_ref)}
    new = {draft.image_id for draft in chosen or ()}
    staged = working_copy.preset_image_ids(copy["creation"] if copy else None)
    return (old | new | staged) - {None}


def apply_presets(connection, story_ref: AuthorizedStory, presets) -> None:
    """5번 프리셋 반영. None(생략)은 저장본을 그대로 두고, []는 모두 비운다.

    구조 오류(없는 id, 남의·GIF 이미지 새 연결)는 예외로 저장 전체를 되돌린다.
    완성 조건은 이어지는 완성 검사가 반영 뒤 저장본 기준으로 모아 알린다.
    """
    if presets is None:
        return
    # TODO(KAN-13): common/ 조회 함수로 교체 (리뷰 server#5) — 1번 캐릭터 이름
    names = [p["name"] for p in repository.read_characters(connection, story_ref.story_id)]
    preset_service.replace_presets(connection, story_ref, presets, character_names=names)


def with_latest_scene(connection, story, body, copy):
    staged = (copy or {}).get("creation")
    story_id = story["id"]
    scene = (
        staged
        if staged is not None
        else to_jsonable_python(
            {
                "fewshots": fewshot_service.read_snapshot(connection, story).fewshots,
                "expression": speech_style.read_expression_fields(connection, story_id),
                "intros": intro_service.read_intros(connection, story_id),
                "relations": initial_state.read_relations(connection, story_id),
                "affinities": initial_state.read_affinities(connection, story_id),
            }
        )
    )
    # 생략한 등급(None)은 다시 검증할 때 직접 보낸 null로 보이지 않게 뺀다(CREATE-US-08).
    unset = {"requested_rating"} if body.requested_rating is None else set()
    payload = body.model_dump(mode="json", exclude=set(SCENE_FIELDS) | unset)
    payload.update({field: scene[field] for field in SCENE_FIELDS})
    if body.kind == "temporary" or body.mode == "auto":
        payload["save_state"] = scene.get("save_state", story["save_state"])
    return CreationSaveRequest.model_validate(payload)


def _save_creation(
    connection, story_id, owner_id, request_id, body, review_coordinator=None, after_commit=None
):
    story_ref = AuthorizedStory(story_id, owner_id)
    digest = request_digest(story_id, body)
    with connection.transaction():
        speech_style.lock_write_request(connection, owner_id, request_id)
        story = speech_style.find_owned_story(connection, story_id, owner_id, lock=True)
        previous = speech_style.find_write_request(connection, owner_id, request_id)
        if previous:
            if previous["request_hash"] != digest:
                raise app_error(409, "IDEMPOTENCY_KEY_REUSED", "같은 요청키의 내용이 다릅니다.")
            return CreationSaveResponse.model_validate(previous["response"]), set()
        copy = working_copy.read(connection, story_id)
        # CREATE-US-07-AC09: 같은 요청키 재전송(위)은 기록한 응답을 돌려준다.
        # 새 확정 요청은 잠금 안에서 그 자동 저장 뒤로 바뀐 것이 없을 때만
        # 진행하고, 거절하면 아무것도 쓰지 않는다.
        auto_key = body.finalize_auto_request_id
        if auto_key is not None and (
            body.save_state != story["save_state"]
            or not autosave_unchanged(connection, story, owner_id, copy, auto_key)
        ):
            raise app_error(
                409,
                "CREATION_AUTOSAVE_CHANGED",
                "자동 저장 뒤 내용이 바뀌었습니다. 다시 확인해 주세요.",
            )
        # CREATE-US-08-AC13: 등급만 담은 임시 저장본 반영도 같은 방식으로 잠금 안에서 확인한다.
        if body.finalize_rating_working_copy_at is not None and not rating_copy_unchanged(
            story, body, copy
        ):
            raise app_error(
                409,
                "CREATION_RATING_WORKING_COPY_CHANGED",
                "등급 임시 저장 뒤 내용이 바뀌었습니다. 다시 확인해 주세요.",
            )
        temporary = body.kind == "temporary" or body.mode == "auto"
        faces = face_entries(body)
        if faces and temporary and story["save_state"] == "draft":
            # CREATE-US-07-AC09: 미등록 스토리의 임시 저장은 본 테이블에 바로 써 새 캐릭터 ID가
            # 일찍 생긴다. 새 캐릭터 사진은 「저장」에서 함께 보내야 한다. 아무것도 쓰지 않는다.
            issue = FieldIssue(
                field="images.characters",
                code="CHARACTER_IMAGE_INDEX_UNSUPPORTED",
                message="미등록 스토리의 새 캐릭터 사진은 「저장」에서 함께 보내 주세요.",
            )
            raise app_error(422, issue.code, issue.message, issues=[issue.model_dump()])
        if body.preserve_scene and temporary and story["save_state"] == "draft":
            if body.basic is not None:
                apply_basic_with_existing_roster(connection, story, body.basic)
            admitted = preset_service.lock_preset_images(
                connection,
                story_ref,
                preset_image_cohort(connection, story_ref, body.presets, copy)
                | explicit_photo_cohort(connection, story_id, body),
            )
            apply_image_references(connection, story_id, owner_id, body, admitted)
            apply_presets(connection, story_ref, body.presets)
            # CREATE-US-08-AC01: 미등록 스토리는 보낸 등급 선택만 본 테이블에 바로 쓴다.
            if body.requested_rating is not None:
                repository.update_requested_rating(connection, story_id, body.requested_rating)
            story.update(repository.update_save_state(connection, story_id, "draft"))
            result = response(connection, story, owner_id)
            result.request_id = request_id
            speech_style.remember_write_request(
                connection, owner_id, request_id, story_id, digest, result.model_dump(mode="json")
            )
            return result, set()
        if body.preserve_scene:
            # 등급 전용 임시본의 장면은 저장 당시 복사본일 뿐이다.
            # 다른 경로에서 바꾼 정본 장면을 되돌리지 않는다.
            scene_copy = None if body.finalize_rating_working_copy_at is not None else copy
            body = with_latest_scene(connection, story, body, scene_copy)
        current, target = story["save_state"], body.save_state
        if temporary and current != "draft":
            # 비공개·공개 스토리의 임시 저장: 이용자가 보는 저장본은 그대로, 임시 저장본에만 쓴다.
            # 검사 없이 원문을 보존한다. 공개 상태 선택(save_state)도 함께 기억한다.
            document = body.model_dump(
                mode="json",
                exclude={
                    "kind",
                    "mode",
                    "presets",
                    "preserve_scene",
                    "basic",
                    "images",
                    "finalize_auto_request_id",
                    "requested_rating",
                    "finalize_rating_working_copy_at",
                },
            )
            # CREATE-US-08-AC13: 등급 선택은 보낸 값을 두고, 빼고 보내면 이미 임시 저장한 선택을
            # 유지한다. 저장본 등급·적용 등급·검토 상태는 바꾸지 않는다.
            creation_doc = copy["creation"] if copy else None
            rating = body.requested_rating or working_copy.staged_requested_rating(creation_doc)
            if rating is not None:
                document[working_copy.REQUESTED_RATING] = rating
            # CREATE-US-08-AC13: 이번 쓰기가 등급만 보낸 preserve_scene 임시 저장이고, 앞의
            # 임시 저장본이 비었거나 등급만 담았고, 사진 임시 저장본이 없을 때만 표시를 남긴다.
            # 문서는 매번 새로 만들므로 다른 임시 저장은 표시를 남기지 않는다(보수적으로 지움).
            if (
                body.preserve_scene
                and body.requested_rating is not None
                and body.basic is None
                and body.presets is None
                and body.images is None
                and (creation_doc is None or working_copy.is_rating_only(creation_doc))
                and (not copy or copy["images"] is None)
            ):
                document[working_copy.RATING_ONLY] = True
            # CREATE-US-07-AC09: 쓰기 전 임시 저장본 기준으로 확정 가능 여부를
            # 정하고, 가능하면 내부 표시(응답에는 싣지 않음)를 남긴다.
            finalizable = (
                body.preserve_scene
                and body.mode == "auto"
                and autosave_finalizable(connection, story, owner_id, copy)
            )
            if finalizable:
                document[AUTOSAVE_FINALIZE] = {"request_id": request_id}
            # 1번 기본 정보는 보낸 원문을 두고, 빼고 보내면 이미 임시 저장한 값을 유지한다.
            basic = (
                body.basic.model_dump(mode="json") if body.basic is not None else staged_basic(copy)
            )
            if basic is not None:
                document["basic"] = basic
                previous_creation = (copy or {}).get("creation")
                if body.basic is not None:
                    document[BASIC_BASELINE] = {
                        "character_ids": sorted(
                            restaged_baseline(connection, story_id, body.basic, previous_creation)
                        )
                    }
                elif previous_creation and BASIC_BASELINE in previous_creation:
                    document[BASIC_BASELINE] = previous_creation[BASIC_BASELINE]
            # CREATE-US-05: 프리셋은 보낸 원문을 두고, 빼고 보내면 이미 임시 저장한 값을 유지한다.
            # []는 "모두 비움"을 임시 저장한 것이며 저장본은 아직 그대로다. 구조 확인(id·이미지
            # 소유)은 하지 않고 원문을 둔다. 「저장」할 때 확인한다.
            presets = (
                [draft.model_dump(mode="json") for draft in body.presets]
                if body.presets is not None
                else working_copy.staged_presets(copy["creation"] if copy else None)
            )
            if presets is not None:
                document["presets"] = presets
            previous_doc = copy["creation"] if copy else None
            # CREATE-US-07-AC09: 새 캐릭터 사진은 그 `basic`과 짝지어 공통 문서에만 둔다.
            # `basic`을 다시 보내면 보낸 사진으로 바꾸고(없으면 비움), 빼면 이전 사진을 유지한다.
            sent_faces = sorted(face_entries(body), key=lambda entry: entry.basic_index)
            staged_faces = (
                [{"basic_index": e.basic_index, "image_ids": list(e.image_ids)} for e in sent_faces]
                if body.basic is not None
                else working_copy.staged_character_images(previous_doc)
            )
            if staged_faces:
                document[working_copy.STAGED_CHARACTER_IMAGES] = staged_faces
            # 6번 사진 임시 저장본에는 기존 캐릭터·표지만 덮어쓴다(새 캐릭터 사진은 위 공통 문서).
            existing_images = existing_character_images(body.images)
            # 임시 프리셋·새 캐릭터 사진의 보호 연결을 바꾸기 전에 이미지 묶음을 id 순으로 잠근다.
            # 이전에 임시 저장한 새 캐릭터 사진도 넣어, 비울 때 미사용 표시를 다시 계산한다.
            admitted = preset_service.lock_preset_images(
                connection,
                story_ref,
                preset_image_cohort(connection, story_ref, body.presets, copy)
                | explicit_photo_cohort(connection, story_id, body)
                | working_copy.staged_character_image_ids(previous_doc)
                | (
                    images_service.working_copy_image_cohort(connection, story_id, copy)
                    if existing_images is not None
                    else set()
                ),
            )
            images_service.check_face_images(
                connection,
                owner_id,
                {i for entry in sent_faces for i in entry.image_ids},
                admitted,
            )
            images_service.stage_combined_images(
                connection, story_ref, existing_images, copy, admitted
            )
            saved_at, relinked = working_copy.write_creation(
                connection, story_id, document, admitted=admitted
            )
            # 이전 데이터의 타인 이미지 연결은 끊되 이미지 행은 정리 작업에 맡긴다.
            images_service.refresh_orphaned_images(connection, relinked & admitted)
            result = working_response(connection, story, owner_id, document, saved_at, request_id)
            result.images_working_copy_saved_at = images_service.images_working_copy_saved_at(
                connection, story_id
            )
            mark_rating_only(result, {"creation": document})
            if finalizable:
                result.finalize_auto_request_id = request_id
            speech_style.remember_write_request(
                connection, owner_id, request_id, story_id, digest, result.model_dump(mode="json")
            )
            return result, set()
        if temporary:
            # 미등록 스토리의 임시 저장은 본 테이블에 바로 쓴다. 공개 상태는 바꾸지 않는다.
            target = current
        elif (current != "draft" and target == "draft") or (
            current == "public" and target == "private"
        ):
            raise app_error(
                409,
                "SAVE_STATE_TRANSITION",
                "비공개·공개한 스토리는 미등록으로, 공개는 비공개로 되돌릴 수 없습니다.",
            )
        # CREATE-US-07: 1번 기본 정보를 먼저 반영해 캐릭터 목록·완성 검사가 그 값 기준이 되게 한다.
        # 보낸 값이 우선이고, 없으면 「저장」일 때만 임시 저장한 값을 쓴다. 미등록 스토리는
        # 임시 저장본이 없어 보낸 값만 바로 쓴다. 검사나 저장이 실패하면 전체가 되돌아가
        # 저장본도 그대로, 임시 저장본도 남는다.
        basic = body.basic
        staged_raw = None
        if basic is None and not temporary:
            staged_raw = staged_basic(copy)
            basic = (
                StoryUpdateRequest.model_validate(staged_raw) if staged_raw is not None else None
            )
        # CREATE-US-07-AC09: 임시 저장한 기본 정보를 반영할 때만 함께 임시 저장한 새 캐릭터 사진을
        # 쓴다. 직접 보낸 `basic`이 있으면 그 사진은 쓰지 않는다(임시 저장본 정리로 보호만 끊긴다).
        staged_doc = copy["creation"] if copy else None
        staged_faces = (
            working_copy.staged_character_images(staged_doc) if staged_raw is not None else []
        )
        removed = set()
        before_ids: set[int] = set()
        if basic is not None and basic.characters is not None and not temporary:
            before_ids = {p["id"] for p in repository.read_characters(connection, story_id)}
            # 새 캐릭터는 직접 보낸 `basic`이면 `images`가 함께 올 때, 임시 저장한 `basic`이면
            # 함께 임시 저장한 새 캐릭터 사진이 있을 때만 허용한다.
            allow_new = body.images is not None if body.basic is not None else bool(staged_faces)
            basic, removed = plan_characters(
                connection,
                story_id,
                basic,
                body.basic is None,
                staged_baseline(copy),
                allow_new=allow_new,
            )
        explicit = (
            []
            if body.images is None
            else [e.character_id for e in body.images.characters if e.character_id is not None]
        )
        # CREATE-US-05: 프리셋은 보낸 값이 우선이고, 없으면 「저장」일 때만 임시 저장한 값을 쓴다.
        # 둘 다 없으면(None) 저장본 프리셋을 그대로 두고 아래 완성 검사가 그 저장본을 검사한다.
        # []는 명시적 비움이다. 미등록 스토리의 임시 저장은 보낸 값만 본 테이블에 바로 쓴다.
        presets = body.presets
        if presets is None and not temporary:
            raw = working_copy.staged_presets(copy["creation"] if copy else None)
            presets = None if raw is None else [PresetDraft.model_validate(p) for p in raw]
        # 첫 이미지 쓰기(사진 임시 저장본 반영·프리셋 교체·임시 저장본 정리) 전에 프리셋 이미지
        # 묶음(저장본·이번 반영·임시 저장본)을 id 순으로 한 번에 잠근다. replace_presets가
        # 같은 행을 다시 잠가도 이미 쥔 잠금이라 기다리지 않는다.
        admitted = preset_service.lock_preset_images(
            connection,
            story_ref,
            preset_image_cohort(connection, story_ref, presets, copy)
            | images_service.working_copy_image_cohort(connection, story_id, copy)
            | (
                images_service.explicit_image_cohort(connection, story_id, explicit)
                if body.images is not None
                else set()
            )
            | requested_image_ids(body)
            | working_copy.staged_character_image_ids(staged_doc)
            | images_service.character_image_ids(connection, removed),
        )
        initial_state.clear_switched_inputs(connection, story_id, body.intros)
        if removed:
            retained = {character.id for character in basic.characters}
            groups, _ = fewshot_service.write_fewshots(connection, story_id, body, retained)
            intros, relations, affinities = write_story_parts(connection, story_id, body, retained)
            detached = images_service.character_image_ids(connection, removed) & admitted
            story["title"] = story_basic.apply_basic(connection, story_id, basic)["title"]
            images_service.refresh_orphaned_images(connection, detached)
            people = fewshot_service.characters(connection, story_id)
        elif basic is not None:
            if temporary:
                apply_basic_with_existing_roster(connection, story, basic)
            else:
                story["title"] = story_basic.apply_basic(connection, story_id, basic)["title"]
        released = (
            set()
            if temporary
            else images_service.apply_working_copy(connection, story_id, admitted)
        )
        if staged_faces:
            # 명단 반영 뒤 새 행 순서로 짝짓고 완성 검사 전에 연결한다. 직접 보낸 `images`가
            # 아래에서 덮어쓰므로 우선한다.
            lists = staged_face_lists(
                connection, story_id, staged_raw, staged_faces, basic, before_ids
            )
            released |= images_service.apply_character_images(connection, owner_id, lists, admitted)
        released |= apply_image_references(connection, story_id, owner_id, body, admitted)
        apply_presets(connection, story_ref, presets)
        if target != "draft":
            validate_completed(connection, story_ref, body.model_copy(update={"basic": basic}))
        if not removed:
            groups, people = fewshot_service.write_fewshots(connection, story_id, body)
            intros, relations, affinities = write_story_parts(
                connection, story_id, body, {p["id"] for p in people}
            )
        speech_style.write_expression_fields(connection, story_id, body.expression)
        # CREATE-US-08-AC01·AC13: 등급 선택은 보낸 값이 우선이고, 없으면 「저장」일 때만 임시
        # 저장한 선택을 반영한다. 둘 다 없으면 저장본 선택을 그대로 둔다. 같은 트랜잭션이라
        # 검사·저장이 실패하면 함께 되돌아간다. 적용 등급·검토 상태·사유·요청 시각은 쓰지 않고
        # 검토를 시작하지도 않는다(후속 조각).
        rating = body.requested_rating
        if rating is None and not temporary:
            rating = working_copy.staged_requested_rating(staged_doc)
        if rating is not None:
            repository.update_requested_rating(connection, story_id, rating)
        story.update(repository.update_save_state(connection, story_id, target))
        # 저장(반영)하면 2~5번 임시 저장본은 더 필요 없다. 6번 사진 임시 저장본은 위에서 반영했다.
        # 임시 프리셋 사진의 보호 연결도 끊고, 미사용 표시를 다시 계산한다(바로 지우지 않고
        # 정리 작업 유예를 따른다).
        unlinked = working_copy.clear_creation(connection, story_id, admitted=admitted)
        images_service.refresh_orphaned_images(connection, unlinked & admitted)
        result = response(
            connection, story, owner_id, groups, people, request_id, intros, relations, affinities
        )
        speech_style.remember_write_request(
            connection, owner_id, request_id, story_id, digest, result.model_dump(mode="json")
        )
        # CREATE-US-08-AC02·AC14: 비공개·공개 「저장」이면 마지막 단계로 정본을 읽고 검토
        # 조정기 울타리에 들어간다(스토리 잠금 안). 이 뒤로는 COMMIT 말고 SQL이 없다.
        # 커밋에 성공하면 울타리를 나오며 알린다. 미등록·임시·자동 저장은 알리지 않는다.
        if review_coordinator is not None and not temporary and target != "draft":
            rating_review.fence_final_save(connection, story_id, review_coordinator, after_commit)
        return result, released
