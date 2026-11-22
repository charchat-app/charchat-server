"""2·3·4·5번 조각을 저장 상태와 함께 하나의 트랜잭션에 반영한다."""

import hashlib
import json
from collections import defaultdict

from pydantic_core import to_jsonable_python

from app.features.creation.common.errors import app_error
from app.features.creation.common.names import has_surrounding_space, name_compare_key
from app.features.creation.images import service as images_service
from app.features.creation.initial_state import service as initial_state
from app.features.creation.intro import service as intro_service
from app.features.creation.save_visibility import repository, working_copy
from app.features.creation.save_visibility.schemas import (
    CreationSaveRequest,
    CreationSaveResponse,
    FieldIssue,
    PresetResponse,
)
from app.features.creation.speech_style import fewshot_service
from app.features.creation.speech_style import service as speech_style
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
        expression=document["expression"],
        intros=document["intros"],
        relations=document["relations"],
        affinities=document["affinities"],
        working_copy_saved_at=saved_at,
        presets=presets,
        presets_pending=staged is not None,
    )


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
        return result


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


def save_creation(connection, story_id, owner_id, request_id, body, storage=None):
    """2~4번 공통 저장. 「저장」이면 이미지 임시 저장본도 함께 검사·반영한다(안 B).

    반영으로 연결이 끊긴 예전 사진은 커밋 뒤 바로 지운다(다른 곳에서 쓰는 사진은 남는다).
    """
    result, released = _save_creation(connection, story_id, owner_id, request_id, body)
    if storage is not None and released:
        images_service.release_images(connection, storage, released)
    return result


def request_digest(story_id, body) -> str:
    """요청키 비교용 해시.

    프리셋을 빼거나 null로 보낸 요청은 그 필드를 빼고 해시해, 프리셋 연결 전과 같은 해시를 쓴다.
    그래서 연결 전에 기록된 요청키 재전송도 기록한 응답을 그대로 돌려받는다.
    """
    exclude = {"presets"} if body.presets is None else set()
    exclude.update(SCENE_FIELDS if body.preserve_scene else ("preserve_scene",))
    payload = {
        "operation": "creation-save-v4",
        "story_id": str(story_id),
        **body.model_dump(mode="json", exclude=exclude),
    }
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
    payload = body.model_dump(mode="json", exclude=set(SCENE_FIELDS))
    payload.update({field: scene[field] for field in SCENE_FIELDS})
    if body.kind == "temporary" or body.mode == "auto":
        payload["save_state"] = scene.get("save_state", story["save_state"])
    return CreationSaveRequest.model_validate(payload)


def _save_creation(connection, story_id, owner_id, request_id, body):
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
        temporary = body.kind == "temporary" or body.mode == "auto"
        if body.preserve_scene and temporary and story["save_state"] == "draft":
            preset_service.lock_preset_images(
                connection,
                story_ref,
                preset_image_cohort(connection, story_ref, body.presets, copy),
            )
            apply_presets(connection, story_ref, body.presets)
            story.update(repository.update_save_state(connection, story_id, "draft"))
            result = response(connection, story, owner_id)
            result.request_id = request_id
            speech_style.remember_write_request(
                connection, owner_id, request_id, story_id, digest, result.model_dump(mode="json")
            )
            return result, set()
        if body.preserve_scene:
            body = with_latest_scene(connection, story, body, copy)
        current, target = story["save_state"], body.save_state
        if temporary and current != "draft":
            # 비공개·공개 스토리의 임시 저장: 이용자가 보는 저장본은 그대로, 임시 저장본에만 쓴다.
            # 검사 없이 원문을 보존한다. 공개 상태 선택(save_state)도 함께 기억한다.
            document = body.model_dump(
                mode="json", exclude={"kind", "mode", "presets", "preserve_scene"}
            )
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
            # 임시 프리셋 사진의 보호 연결을 바꾸기 전에 프리셋 이미지 묶음을 id 순으로 잠근다.
            admitted = preset_service.lock_preset_images(
                connection,
                story_ref,
                preset_image_cohort(connection, story_ref, body.presets, copy),
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
            | images_service.working_copy_image_cohort(connection, story_id, copy),
        )
        # 방식·시간제를 바꾼 장면의 이전 입력을 비운 뒤 검사·저장한다 (CREATE-US-04-AC07).
        initial_state.clear_switched_inputs(connection, story_id, body.intros)
        # 안 B: 사진 임시 저장본을 먼저 반영해, 완성 검사가 편집 화면의 사진 기준으로 하게 한다.
        # 검사나 저장이 실패하면 트랜잭션 전체가 되돌아가 사진도 그대로, 임시 저장본도 남는다.
        released = images_service.apply_working_copy(connection, story_id)
        # 완성 검사 전에 프리셋도 반영한다(실패하면 함께 롤백).
        apply_presets(connection, story_ref, presets)
        if target != "draft":
            validate_completed(connection, story_ref, body)
        groups, people = fewshot_service.write_fewshots(connection, story_id, body)
        character_ids = {p["id"] for p in people}
        intros = intro_service.write_intros(connection, story_id, body.intros, character_ids)
        relations = initial_state.write_relations(
            connection, story_id, body.relations, character_ids
        )
        affinities = initial_state.write_affinities(
            connection, story_id, body.affinities, character_ids
        )
        speech_style.write_expression_fields(connection, story_id, body.expression)
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
        return result, released
