"""CREATE-US-01 스토리 기본 정보(제목·한 줄 소개·세계관·해시태그)와 캐릭터 저장·조회 로직."""

from collections import defaultdict
from dataclasses import dataclass

from psycopg import Connection

from app.core import common_safety
from app.core.common_safety import CommonSafetyGuard
from app.core.exceptions import AppError
from app.features.creation.common.names import has_surrounding_space, name_compare_key
from app.features.creation.story_basic import repository as repo
from app.features.creation.story_basic.schemas import (
    CharacterResponse,
    CharacterWriteItem,
    FieldIssue,
    PublicStorySummaryResponse,
    StoryCreateRequest,
    StoryResponse,
    StorySummaryResponse,
    StoryUpdateRequest,
    TagSuggestionResponse,
)

DRAFT = "draft"
MIN_CHARACTERS = 1
MAX_CHARACTERS = 10
MAX_TAGS = 10
MAX_TAG_LENGTH = 10
# 제목·한 줄 소개·세계관과 모든 캐릭터 이름·상세의 합산 글자 수 한도 (CREATE-US-01-AC09)
MAX_BASIC_TEXT_LENGTH = 3000
TAG_SUGGESTION_LIMIT = 20
TAG_SEARCH_LIMIT = 50

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


def collect_tag_issues(tags: list[str]) -> list[FieldIssue]:
    """해시태그 등록 조건 검사. 해시태그는 선택 입력이라 0개는 문제가 아니다 (AC06).

    - AC15: 최대 10개 (태그 길이 제한과 별개)
    - AC13: 태그 하나당 10자. 공백 포함 원문 글자 수(유니코드 코드 포인트)로 센다.
      이모지·결합 문자의 글자 단위는 미결이라 코드 포인트 기준을 임시로 쓴다.
    - 빈 태그(공백만 있는 값)는 유효한 태그가 아니다.
    - 대소문자·띄어쓰기만 다른 태그(예: Romance·romance)는 같은 태그라 한 스토리에 한 번만 쓴다.
    `tags[i]`의 i는 넘겨받은 목록 기준이라 응답마다 다를 수 있다. 태그는 `value`(원문)로 찾는다.
    """
    issues: list[FieldIssue] = []
    if len(tags) > MAX_TAGS:
        issues.append(
            FieldIssue(
                field="tags",
                code="TAG_COUNT_EXCEEDED",
                message=(
                    f"해시태그는 최대 {MAX_TAGS}개까지 등록할 수 있습니다. (현재 {len(tags)}개)"
                ),
            )
        )
    seen: dict[str, int] = {}
    for i, tag in enumerate(tags):
        if _is_blank(tag):
            issues.append(
                FieldIssue(
                    field=f"tags[{i}]", code="TAG_BLANK", message="빈 해시태그입니다.", value=tag
                )
            )
        elif len(tag) > MAX_TAG_LENGTH:
            issues.append(
                FieldIssue(
                    field=f"tags[{i}]",
                    code="TAG_TOO_LONG",
                    value=tag,
                    message=(
                        f"'{tag}'는 {len(tag)}자예요. "
                        f"해시태그는 {MAX_TAG_LENGTH}자까지 쓸 수 있습니다."
                    ),
                )
            )
        key = name_compare_key(strip_tag_mark(tag))
        if key and key in seen:
            issues.append(
                FieldIssue(
                    field=f"tags[{i}]",
                    code="TAG_DUPLICATE",
                    value=tag,
                    message=(
                        f"'{tag}'는 '{tags[seen[key]]}'와 같은 태그로 취급됩니다. "
                        "영문 대소문자와 띄어쓰기는 구분하지 않습니다."
                    ),
                )
            )
        else:
            seen.setdefault(key, i)
    return issues


def strip_tag_mark(text: str) -> str:
    """태그 표기를 정리한다: 앞뒤 공백 → 앞쪽 해시 기호(#) → 다시 앞뒤 공백 순으로 뗀다.

    '#로맨스'·'##로맨스'·' #로맨스'·'# 로맨스' → '로맨스'. 앱(normalizeTagInput)과 같은 규칙이다.
    #과 앞뒤 공백은 입력 구분용이라 태그 내용이 아니다. 태그 안의 공백·대소문자는 바꾸지 않는다.
    """
    return text.strip().lstrip("#").strip()


def basic_text_length(
    title: str | None,
    subtitle: str | None,
    world_description: str | None,
    characters: list[CharacterDraft],
) -> int:
    """AC09 합산 글자 수: 제목·한 줄 소개·세계관 + 모든 캐릭터 이름·상세.

    공백·줄바꿈 포함, 유니코드 코드 포인트 단위(앱과 같은 기준). 해시태그·퓨샷·인트로는 제외한다.
    """
    parts = [title, subtitle, world_description]
    for ch in characters:
        parts += [ch.name, ch.description]
    return sum(len(p) for p in parts if p)


def collect_length_issues(
    title: str | None,
    subtitle: str | None,
    world_description: str | None,
    characters: list[CharacterDraft],
) -> list[FieldIssue]:
    """CREATE-US-01-AC09: 합산 3000자 초과 안내. 입력은 자르지 않고 임시 저장은 허용한다."""
    total = basic_text_length(title, subtitle, world_description, characters)
    if total <= MAX_BASIC_TEXT_LENGTH:
        return []
    return [
        FieldIssue(
            field="basic_text",
            code="BASIC_TEXT_TOO_LONG",
            value=str(total),
            message=(
                f"기본 설정과 캐릭터 글자 수 합계가 {total:,}자예요. "
                f"{MAX_BASIC_TEXT_LENGTH:,}자 이내로 줄여 주세요. "
                f"({total - MAX_BASIC_TEXT_LENGTH:,}자 초과)"
            ),
        )
    ]


def _dedupe(names: list[str]) -> list[str]:
    """표기를 정리(strip_tag_mark)한 뒤 완전히 같은 표기만 하나로 합친다.

    정리하고 나면 비는 값('#'만, 공백만)은 태그로 저장하지 않는다.
    """
    return list(dict.fromkeys(s for s in (strip_tag_mark(n) for n in names) if s))


def _to_response(conn: Connection, row: repo.Row) -> StoryResponse:
    characters = [CharacterResponse(**c) for c in repo.list_characters(conn, row["id"])]
    issues = collect_basic_issues(row["title"], row["subtitle"], row["world_description"])
    drafts = [CharacterDraft(c.name, c.description) for c in characters]
    issues += collect_character_issues(drafts)
    issues += collect_length_issues(row["title"], row["subtitle"], row["world_description"], drafts)
    tags = repo.list_story_tags(conn, row["id"])
    issues += collect_tag_issues(tags)
    return StoryResponse(
        id=row["id"],
        save_state=row["save_state"],
        title=row["title"],
        subtitle=row["subtitle"],
        world_description=row["world_description"],
        tags=tags,
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
        tags = repo.list_story_tags(conn, story_id) if req.tags is None else _dedupe(req.tags)
        issues = collect_basic_issues(req.title, req.subtitle, req.world_description)
        issues += collect_character_issues(characters)
        issues += collect_length_issues(req.title, req.subtitle, req.world_description, characters)
        issues += collect_tag_issues(tags)
        if story["save_state"] != DRAFT and issues:
            raise StoryIncompleteError(
                "공개·비공개로 저장한 스토리는 저장 조건을 모두 충족해야 저장할 수 있습니다.",
                issues=[i.model_dump() for i in issues],
            )
        return _to_response(conn, apply_basic(conn, story_id, req))


def apply_basic(conn: Connection, story_id: int, req: StoryUpdateRequest) -> repo.Row:
    """기본 정보·캐릭터를 검사 없이 반영하고 스토리 행을 돌려준다. 트랜잭션은 호출 측이 연다.

    해시태그는 표기를 정리(strip_tag_mark)해 저장한다. 완성 조건 검사·거절은 호출 측 책임이다.
    CREATE-US-07 공통 저장(save_visibility)이 다른 기능의 repository를 직접 쓰지 않도록 연다.
    """
    row = repo.update_story_basic(conn, story_id, req.title, req.subtitle, req.world_description)
    if req.tags is not None:
        repo.replace_story_tags(conn, story_id, _dedupe(req.tags))
    if req.characters is not None:
        _sync_characters(conn, story_id, req.characters)
    return row


def suggest_tags(conn: Connection, query: str) -> list[TagSuggestionResponse]:
    """AC17: 제작 화면의 해시태그 자동검색.

    대소문자·띄어쓰기를 무시한 비교 키의 앞부분 일치, 공개 스토리 수 많은 순.
    """
    key = name_compare_key(strip_tag_mark(query))
    if key == "":
        return []
    rows = repo.suggest_tags(conn, key, TAG_SUGGESTION_LIMIT)
    return [TagSuggestionResponse(**r) for r in rows]


def search_public_stories_by_tag(
    conn: Connection, tag: str, guard: CommonSafetyGuard | None = None
) -> list[PublicStorySummaryResponse]:
    """AC16: 해시태그로 공개 스토리를 찾는다. 대소문자·띄어쓰기를 무시하고 같은 태그로 본다.

    노출 순위·비중은 탐색 기능에서 정한다.
    CREATE-US-08-AC15: 판정기를 주입하면 결과와 같은 스냅숏의 정본으로 스토리마다 판정해
    통과한 스토리만 돌려준다(차단·판정 불가는 조용히 뺀다). 결과는 캐시하지 않는다.
    """
    key = name_compare_key(strip_tag_mark(tag))
    if key == "":
        return []
    if guard is None:
        rows = repo.list_public_stories_by_tag(conn, key, TAG_SEARCH_LIMIT)
        return [PublicStorySummaryResponse(**r) for r in rows]
    with conn.transaction():
        repo.set_consistent_read(conn)
        rows = repo.list_public_stories_by_tag(conn, key, TAG_SEARCH_LIMIT)
        return [
            PublicStorySummaryResponse(**r)
            for r in rows
            if common_safety.story_is_clear(conn, guard, r["id"])
        ]
