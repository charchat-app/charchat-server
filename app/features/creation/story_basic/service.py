"""CREATE-US-01 스토리 기본 정보(제목·한 줄 소개·세계관·해시태그) 저장·조회 로직."""

from psycopg import Connection

from app.core.exceptions import AppError
from app.features.creation.story_basic import repository as repo
from app.features.creation.story_basic.schemas import (
    FieldIssue,
    StoryCreateRequest,
    StoryResponse,
    StorySummaryResponse,
    StoryUpdateRequest,
)

DRAFT = "draft"

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


def _dedupe(names: list[str]) -> list[str]:
    """완전히 같은 표기만 하나로 합친다. 원문을 trim·대소문자 변환하지 않는다."""
    return list(dict.fromkeys(names))


def _to_response(conn: Connection, row: repo.Row) -> StoryResponse:
    return StoryResponse(
        id=row["id"],
        save_state=row["save_state"],
        title=row["title"],
        subtitle=row["subtitle"],
        world_description=row["world_description"],
        tags=repo.list_story_tags(conn, row["id"]),
        saved_at=row["saved_at"],
        issues=collect_basic_issues(row["title"], row["subtitle"], row["world_description"]),
    )


def _get_owned_story(
    conn: Connection, owner_id: int, story_id: int, *, for_update: bool = False
) -> repo.Row:
    """본인의, 삭제되지 않은 스토리만 돌려준다. 타인 스토리는 존재 여부를 드러내지 않는다."""
    row = repo.find_story(conn, story_id, for_update=for_update)
    if row is None or row["owner_id"] != owner_id or row["deleted_at"] is not None:
        raise StoryNotFoundError(f"스토리 {story_id}를 찾을 수 없습니다.")
    return row


def create_story(conn: Connection, owner_id: int, req: StoryCreateRequest) -> StoryResponse:
    """새 스토리를 임시 저장(draft) 상태로 만든다. 미완성 입력도 그대로 저장한다."""
    with conn.transaction():
        row = repo.insert_story(conn, owner_id, req.title, req.subtitle, req.world_description)
        repo.replace_story_tags(conn, row["id"], _dedupe(req.tags))
        return _to_response(conn, row)


def list_stories(conn: Connection, owner_id: int) -> list[StorySummaryResponse]:
    """제작자 본인의 스토리 목록. 저장한 스토리를 다시 열기 위한 진입점이다."""
    return [StorySummaryResponse(**r) for r in repo.list_owned_stories(conn, owner_id)]


def get_story(conn: Connection, owner_id: int, story_id: int) -> StoryResponse:
    """CREATE-US-01-AC01: 마지막으로 저장된 기본 정보를 돌려준다."""
    return _to_response(conn, _get_owned_story(conn, owner_id, story_id))


def update_story(
    conn: Connection, owner_id: int, story_id: int, req: StoryUpdateRequest
) -> StoryResponse:
    """기본 정보를 한 트랜잭션으로 저장한다.

    - draft: 필수값이 비어 있어도 저장하고 `issues`로 알려 준다 (AC02 입력 보존).
    - private/public: 완성 저장된 스토리는 조건 위반이면 아무것도 바꾸지 않고 거절한다.
      저장 종류 전환 자체는 CREATE-US-07에서 다룬다.
    """
    with conn.transaction():
        story = _get_owned_story(conn, owner_id, story_id, for_update=True)
        issues = collect_basic_issues(req.title, req.subtitle, req.world_description)
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
        return _to_response(conn, row)
