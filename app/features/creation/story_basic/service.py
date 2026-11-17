"""CREATE-US-01 스토리 기본 정보(제목·한 줄 소개·세계관·해시태그)와 캐릭터 저장·조회 로직."""

from collections import defaultdict
from dataclasses import dataclass

from psycopg import Connection

from app.core.exceptions import AppError
from app.features.creation.common.names import has_surrounding_space, name_compare_key
from app.features.creation.story_basic import repository as repo
from app.features.creation.story_basic.schemas import (
    CharacterResponse,
    CharacterWriteItem,
    FieldIssue,
    StoryCreateRequest,
    StoryResponse,
    StorySummaryResponse,
    StoryUpdateRequest,
)

DRAFT = "draft"
MIN_CHARACTERS = 1
MAX_CHARACTERS = 10

# 해시태그는 선택 입력이라 여기에 없다 (CREATE-US-01-AC06).
REQUIRED_BASIC_FIELDS = {
    "title": "제목을 입력하세요.",
    "subtitle": "한 줄 소개를 입력하세요.",
    "world_description": "세계관을 입력하세요.",
}


class StoryNotFoundError(AppError):
    status = 404
    code = "STORY_NOT_FOUND"
    title = "스토리를 찾을 수 없습니다"


class StoryIncompleteError(AppError):
    status = 422
    code = "STORY_INCOMPLETE"
    title = "저장 조건을 충족하지 않습니다"


class InvalidCharacterReferenceError(AppError):
    status = 422
    code = "INVALID_CHARACTER_REFERENCE"
    title = "캐릭터 목록이 올바르지 않습니다"


@dataclass(frozen=True)
class CharacterDraft:
    name: str | None
    description: str | None


def _is_blank(value: str | None) -> bool:
    return value is None or value.strip() == ""


def collect_basic_issues(
    title: str | None, subtitle: str | None, world_description: str | None
) -> list[FieldIssue]:
    """CREATE-US-01-AC02: 필수 기본 정보 누락 검사. 공백만 있는 값도 누락으로 본다."""
    values = {"title": title, "subtitle": subtitle, "world_description": world_description}
    return [
        FieldIssue(field=field, code="REQUIRED", message=message)
        for field, message in REQUIRED_BASIC_FIELDS.items()
        if _is_blank(values[field])
    ]


def collect_character_issues(characters: list[CharacterDraft]) -> list[FieldIssue]:
    """캐릭터 등록 조건 검사. 임시 저장은 막지 않고, 완성 저장 판단에 쓴다.

    - AC08: 최소 1명·최대 10명
    - AC05: 이름·상세 필수 (공백만 있는 값도 누락)
    - AC11: 이름 앞뒤 띄어쓰기는 형식 오류 (자동으로 자르지 않음)
    - AC10: 영문 대소문자·띄어쓰기를 무시하고 같은 이름은 중복
    유저 캐릭터 이름과의 중복은 CREATE-US-05에서 같은 규칙(common/names.py)으로 이어 검사한다.
    """
    issues: list[FieldIssue] = []
    if not MIN_CHARACTERS <= len(characters) <= MAX_CHARACTERS:
        issues.append(
            FieldIssue(
                field="characters",
                code="CHARACTER_COUNT_OUT_OF_RANGE",
                message=(
                    f"캐릭터는 {MIN_CHARACTERS}명 이상 {MAX_CHARACTERS}명 이하로 등록하세요. "
                    f"(현재 {len(characters)}명)"
                ),
            )
        )

    by_key: dict[str, list[int]] = defaultdict(list)
    for i, ch in enumerate(characters):
        path = f"characters[{i}]"
        if _is_blank(ch.name):
            issues.append(
                FieldIssue(field=f"{path}.name", code="REQUIRED", message="이름을 입력하세요.")
            )
        elif has_surrounding_space(ch.name):
            issues.append(
                FieldIssue(
                    field=f"{path}.name",
                    code="NAME_SURROUNDING_SPACE",
                    message="이름 앞뒤에는 띄어쓰기를 쓸 수 없습니다.",
                )
            )
        else:
            by_key[name_compare_key(ch.name)].append(i)
        if _is_blank(ch.description):
            issues.append(
                FieldIssue(
                    field=f"{path}.description", code="REQUIRED", message="상세를 입력하세요."
                )
            )

    for indexes in by_key.values():
        if len(indexes) < 2:
            continue
        for i in indexes:
            issues.append(
                FieldIssue(
                    field=f"characters[{i}].name",
                    code="NAME_DUPLICATE",
                    message=(
                        "같은 스토리에 같은 이름이 있습니다. "
                        "영문 대소문자와 띄어쓰기는 구분하지 않습니다."
                    ),
                )
            )
    return issues


def _dedupe(names: list[str]) -> list[str]:
    """완전히 같은 표기만 하나로 합친다. 원문을 trim·대소문자 변환하지 않는다."""
    return list(dict.fromkeys(names))


def _to_response(conn: Connection, row: repo.Row) -> StoryResponse:
    characters = [CharacterResponse(**c) for c in repo.list_characters(conn, row["id"])]
    issues = collect_basic_issues(row["title"], row["subtitle"], row["world_description"])
    issues += collect_character_issues([CharacterDraft(c.name, c.description) for c in characters])
    return StoryResponse(
        id=row["id"],
        save_state=row["save_state"],
        title=row["title"],
        subtitle=row["subtitle"],
        world_description=row["world_description"],
        tags=repo.list_story_tags(conn, row["id"]),
        characters=characters,
        saved_at=row["saved_at"],
        issues=issues,
    )


def _get_owned_story(
    conn: Connection, owner_id: int, story_id: int, *, for_update: bool = False
) -> repo.Row:
    """본인의, 삭제되지 않은 스토리만 돌려준다. 타인 스토리는 존재 여부를 드러내지 않는다."""
    row = repo.find_story(conn, story_id, for_update=for_update)
    if row is None or row["owner_id"] != owner_id or row["deleted_at"] is not None:
        raise StoryNotFoundError(f"스토리 {story_id}를 찾을 수 없습니다.")
    return row


def _sync_characters(conn: Connection, story_id: int, items: list[CharacterWriteItem]) -> None:
    """요청 목록을 스토리의 전체 캐릭터로 맞춘다 (AC12). 목록 순서가 position이 된다.

    기존 캐릭터는 id로 그대로 수정해 이미지·말풍선 등 연결을 유지하고(AC04),
    목록에서 빠진 캐릭터만 삭제한다. 다른 스토리의 캐릭터 id는 거절한다.
    """
    existing = repo.list_character_ids(conn, story_id)
    given = [it.id for it in items if it.id is not None]
    unknown = [i for i in given if i not in existing]
    if unknown:
        raise InvalidCharacterReferenceError(
            "이 스토리에 없는 캐릭터 id가 있습니다.", character_ids=unknown
        )
    if len(given) != len(set(given)):
        raise InvalidCharacterReferenceError("같은 캐릭터 id가 목록에 두 번 이상 있습니다.")

    repo.delete_characters_except(conn, story_id, given)
    for position, it in enumerate(items):
        if it.id is None:
            repo.insert_character(conn, story_id, it.name, it.description, position)
        else:
            repo.update_character(conn, it.id, it.name, it.description, position)


def list_stories(conn: Connection, owner_id: int) -> list[StorySummaryResponse]:
    """제작자 본인의 스토리 목록. 저장한 스토리를 다시 열기 위한 진입점이다."""
    return [StorySummaryResponse(**r) for r in repo.list_owned_stories(conn, owner_id)]


def create_story(conn: Connection, owner_id: int, req: StoryCreateRequest) -> StoryResponse:
    """새 스토리를 임시 저장(draft) 상태로 만든다. 미완성 입력도 그대로 저장한다."""
    if any(c.id is not None for c in req.characters):
        raise InvalidCharacterReferenceError("새 스토리의 캐릭터에는 id를 넣지 않습니다.")
    with conn.transaction():
        row = repo.insert_story(conn, owner_id, req.title, req.subtitle, req.world_description)
        repo.replace_story_tags(conn, row["id"], _dedupe(req.tags))
        _sync_characters(conn, row["id"], req.characters)
        return _to_response(conn, row)


def get_story(conn: Connection, owner_id: int, story_id: int) -> StoryResponse:
    """CREATE-US-01-AC01·AC04: 마지막으로 저장된 기본 정보와 캐릭터를 돌려준다."""
    return _to_response(conn, _get_owned_story(conn, owner_id, story_id))


def update_story(
    conn: Connection, owner_id: int, story_id: int, req: StoryUpdateRequest
) -> StoryResponse:
    """기본 정보·캐릭터를 한 트랜잭션으로 저장한다.

    - draft: 조건 위반이 있어도 저장하고 `issues`로 알려 준다 (입력 보존).
    - private/public: 완성 저장된 스토리는 조건 위반이면 아무것도 바꾸지 않고 거절한다.
      저장 종류 전환 자체는 CREATE-US-07에서 다룬다.
    """
    with conn.transaction():
        story = _get_owned_story(conn, owner_id, story_id, for_update=True)
        if req.characters is None:
            characters = [
                CharacterDraft(c["name"], c["description"])
                for c in repo.list_characters(conn, story_id)
            ]
        else:
            characters = [CharacterDraft(c.name, c.description) for c in req.characters]
        issues = collect_basic_issues(req.title, req.subtitle, req.world_description)
        issues += collect_character_issues(characters)
        if story["save_state"] != DRAFT and issues:
            raise StoryIncompleteError(
                "공개·비공개로 저장한 스토리는 저장 조건을 모두 충족해야 저장할 수 있습니다.",
                issues=[i.model_dump() for i in issues],
            )
        row = repo.update_story_basic(
            conn, story_id, req.title, req.subtitle, req.world_description
        )
        if req.tags is not None:
            repo.replace_story_tags(conn, story_id, _dedupe(req.tags))
        if req.characters is not None:
            _sync_characters(conn, story_id, req.characters)
        return _to_response(conn, row)
