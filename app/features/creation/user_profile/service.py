"""CREATE-US-05 유저 프로필 프리셋 검증과 읽기·교체 로직.

호출 규약 (이 모듈은 라우터·엔드포인트가 아니라 저장 서비스가 조합해 쓰는 부품이다):
- 호출자가 연 트랜잭션 안에서만 쓴다. 커밋·롤백·연결 생성을 하지 않는다.
- 호출자가 인가를 마친 `AuthorizedStory`만 받는다. 인증·스토리 인가는 판단하지 않는다.
- 비교 대상 캐릭터 이름은 호출자가 후보(이번 저장에 들어갈 값)로 명시해 넘긴다.
  다른 스토리 폴더의 테이블이나 코드를 읽지 않는다.
- `drafts=None`은 "요청에 프리셋이 없음(생략)"이라 DB를 건드리지 않는다.
  `drafts=[]`는 "프리셋을 모두 비움"이라 기존 프리셋을 모두 삭제한다.
- 스토리 `saved_at` 갱신과 공개 상태 전환은 호출하는 저장 서비스(CREATE-US-07) 몫이다.
"""

from collections import defaultdict
from collections.abc import Sequence

from psycopg import Connection
from psycopg.pq import TransactionStatus

from app.core.exceptions import AppError
from app.features.creation.common.names import has_surrounding_space, name_compare_key
from app.features.creation.images import service as images_service
from app.features.creation.user_profile import repository as repo
from app.features.creation.user_profile.schemas import (
    AuthorizedStory,
    PresetDraft,
    PresetIssue,
    PresetRecord,
    PresetSaveResult,
)

MAX_PRESETS = 5
# 프리셋 하나의 이름+상세 원문 글자 수 한도. 공백·줄바꿈·`{{user}}` 원문 포함, 코드 포인트 단위.
MAX_PRESET_TEXT_LENGTH = 1000
# 이름이 정확히 이 값이면 치환하지 않고 원문 그대로 보존하는 자리표시자다.
USER_PLACEHOLDER = "{{user}}"


class PresetStoryNotFoundError(AppError):
    status = 404
    code = "STORY_NOT_FOUND"
    title = "스토리를 찾을 수 없습니다"


class InvalidPresetReferenceError(AppError):
    status = 422
    code = "INVALID_PRESET_REFERENCE"
    title = "프리셋 목록이 올바르지 않습니다"


class InvalidPresetImageError(AppError):
    status = 422
    code = "INVALID_PRESET_IMAGE"
    title = "사용할 수 없는 이미지입니다"


def _is_blank(value: str | None) -> bool:
    return value is None or value.strip() == ""


def is_user_placeholder(name: str | None) -> bool:
    return name == USER_PLACEHOLDER


def preset_text_length(draft: PresetDraft) -> int:
    """이름+상세 원문 글자 수. 앞뒤 공백도 센다."""
    return sum(len(p) for p in (draft.name, draft.description) if p)


def collect_preset_issues(
    drafts: Sequence[PresetDraft], character_names: Sequence[str | None]
) -> list[PresetIssue]:
    """완성 저장 조건 검사. 초안 저장은 막지 않고, 입력은 자르거나 고치지 않는다.

    - 개수는 0~5개. 0개는 문제가 아니다.
    - 제공한 프리셋마다 이름과 이미지가 필수다 (공백만 있는 이름은 누락). 상세는 선택이다.
    - 이름 앞뒤 띄어쓰기는 형식 오류다 (common/names.py).
    - 이름+상세 원문은 프리셋마다 1000자 이하다.
    - 직접 이름은 같은 스토리의 캐릭터 이름·다른 직접 이름과 비교 키(common/names.py)가
      같으면 중복이다. `{{user}}`는 이름 비교에서 빼며 여러 프리셋에 써도 된다.
    """
    issues: list[PresetIssue] = []
    if len(drafts) > MAX_PRESETS:
        issues.append(
            PresetIssue(
                field="presets",
                code="PRESET_COUNT_EXCEEDED",
                message=(
                    f"프리셋은 최대 {MAX_PRESETS}개까지 등록할 수 있습니다. (현재 {len(drafts)}개)"
                ),
            )
        )

    character_keys = {name_compare_key(n) for n in character_names if not _is_blank(n)}
    direct_by_key: dict[str, list[int]] = defaultdict(list)

    for i, d in enumerate(drafts):
        path = f"presets[{i}]"
        if _is_blank(d.name):
            issues.append(
                PresetIssue(field=f"{path}.name", code="REQUIRED", message="이름을 입력하세요.")
            )
        elif not is_user_placeholder(d.name):
            if has_surrounding_space(d.name):
                issues.append(
                    PresetIssue(
                        field=f"{path}.name",
                        code="NAME_SURROUNDING_SPACE",
                        message="이름 앞뒤에는 띄어쓰기를 쓸 수 없습니다.",
                    )
                )
            else:
                direct_by_key[name_compare_key(d.name)].append(i)
        if d.image_id is None:
            issues.append(
                PresetIssue(
                    field=f"{path}.image_id", code="REQUIRED", message="이미지를 등록하세요."
                )
            )
        total = preset_text_length(d)
        if total > MAX_PRESET_TEXT_LENGTH:
            issues.append(
                PresetIssue(
                    field=f"{path}.text",
                    code="PRESET_TEXT_TOO_LONG",
                    value=str(total),
                    message=(
                        f"이름과 상세 글자 수 합계가 {total:,}자예요. "
                        f"{MAX_PRESET_TEXT_LENGTH:,}자 이내로 줄여 주세요. "
                        f"({total - MAX_PRESET_TEXT_LENGTH:,}자 초과)"
                    ),
                )
            )

    rule = "영문 대소문자와 띄어쓰기는 구분하지 않습니다."
    for key, indexes in direct_by_key.items():
        if key in character_keys:
            for i in indexes:
                issues.append(
                    PresetIssue(
                        field=f"presets[{i}].name",
                        code="NAME_DUPLICATE_CHARACTER",
                        message=f"같은 스토리의 캐릭터와 같은 이름입니다. {rule}",
                    )
                )
        if len(indexes) > 1:
            for i in indexes:
                issues.append(
                    PresetIssue(
                        field=f"presets[{i}].name",
                        code="NAME_DUPLICATE_PRESET",
                        message=f"다른 프리셋과 같은 이름입니다. {rule}",
                    )
                )
    return issues


def _require_caller_transaction(conn: Connection) -> None:
    """교체는 여러 문장을 쓰므로 호출자가 연 트랜잭션 안에서만 허용한다 (개발 오류 방지)."""
    if conn.info.transaction_status != TransactionStatus.INTRANS:
        raise RuntimeError("replace_presets는 호출자의 트랜잭션 안에서만 호출한다.")


def _check_preset_ids(existing_ids: set[int], drafts: Sequence[PresetDraft]) -> None:
    """수정·순서 변경하는 id는 이 스토리의 기존 프리셋이어야 하고 한 번만 나와야 한다."""
    given = [d.id for d in drafts if d.id is not None]
    if len(given) != len(set(given)):
        raise InvalidPresetReferenceError("같은 프리셋 id가 두 번 이상 있습니다.")
    unknown = [i for i in given if i not in existing_ids]
    if unknown:
        raise InvalidPresetReferenceError(
            "이 스토리에 없는 프리셋 id가 있습니다.", preset_ids=unknown
        )


def _check_attached_images(
    conn: Connection,
    story: AuthorizedStory,
    existing: dict[int, PresetRecord],
    drafts: Sequence[PresetDraft],
) -> None:
    """새로 연결하는 이미지만 확인한다: 존재하고, 스토리 제작자의 것이며, GIF가 아니어야 한다.

    GIF는 스토리 이미지에만 쓸 수 있다(CREATE-US-06 AC05). 확인한 이미지 행은 잠가 두어,
    첫 쓰기 전에 정리 작업이 지우지 못하게 한다(repository.find_image_owners).
    이미 그 프리셋에 연결돼 있던 이미지는 다시 확인하지 않는다. 업로드 성공·용량은
    업로드 서비스(CREATE-US-06) 책임이라 여기서 판단하지 않는다.
    존재하지 않는 이미지와 남의 이미지는 같은 오류로 알려 존재 여부를 드러내지 않는다.
    """
    attached = [
        d.image_id
        for d in drafts
        if d.image_id is not None and (d.id is None or existing[d.id].image_id != d.image_id)
    ]
    touched = {r.image_id for r in existing.values() if r.image_id is not None}
    touched.update(attached)
    if not touched:
        return
    # 연결을 끊는 이미지도 먼저 잠가, 다른 트랜잭션의 마지막 연결 변경과 순서를 맞춘다.
    owners = repo.find_image_owners(conn, sorted(touched))
    bad = sorted({i for i in attached if owners.get(i) != story.owner_id})
    if bad:
        raise InvalidPresetImageError("사용할 수 없는 이미지가 있습니다.", image_ids=bad)


def _to_records(rows: Sequence[dict]) -> list[PresetRecord]:
    return [PresetRecord(**r) for r in rows]


def read_presets(conn: Connection, story: AuthorizedStory) -> list[PresetRecord]:
    """저장된 프리셋을 편집 순서대로 돌려준다. 이름·이미지가 비어 있는 초안도 그대로 돌려준다."""
    return _to_records(repo.list_presets(conn, story.story_id))


def replace_presets(
    conn: Connection,
    story: AuthorizedStory,
    drafts: Sequence[PresetDraft] | None,
    *,
    character_names: Sequence[str | None],
) -> PresetSaveResult | None:
    """스토리의 프리셋 전체를 요청 목록으로 맞춘다. 목록 순서가 편집 순서(position)가 된다.

    - `drafts=None`: 생략. DB를 건드리지 않고 None을 돌려준다.
    - `drafts=[]`: 명시적 비움. 기존 프리셋을 모두 삭제한다.
    - id가 있는 항목은 같은 id로 수정해 id를 유지한다. 목록에서 빠진 프리셋만 삭제한다.
    - 구조 오류(이 스토리에 없는 id, 중복 id, 사용할 수 없는 이미지)는 쓰기 전에 예외로
      거절한다. 완성 조건 위반은 원문 그대로 저장하고 `issues`로 알려 준다.
      공개·비공개 저장에서 `issues`를 막을지는 호출하는 저장 서비스가 정한다.
    - 쓰는 도중 실패해도 부분 저장이 남지 않도록, 예외는 호출자의 트랜잭션 밖으로 전파해
      롤백되게 해야 한다.
    """
    if drafts is None:
        return None
    _require_caller_transaction(conn)
    if not repo.lock_story(conn, story.story_id, story.owner_id):
        raise PresetStoryNotFoundError(f"스토리 {story.story_id}를 찾을 수 없습니다.")

    existing = {r.id: r for r in read_presets(conn, story)}
    _check_preset_ids(set(existing), drafts)
    _check_attached_images(conn, story, existing, drafts)
    issues = collect_preset_issues(drafts, character_names)

    repo.delete_presets_except(conn, story.story_id, [d.id for d in drafts if d.id is not None])
    for position, d in enumerate(drafts):
        if d.id is None:
            repo.insert_preset(conn, story.story_id, d.name, d.image_id, d.description, position)
        elif not repo.update_preset(
            conn, story.story_id, d.id, d.name, d.image_id, d.description, position
        ):
            raise InvalidPresetReferenceError("프리셋을 수정하지 못했습니다.", preset_ids=[d.id])

    # 끊긴 예전 이미지와 새로 연결된 이미지의 미사용 표시를 같은 트랜잭션 안에서 다시 계산한다.
    touched = {r.image_id for r in existing.values()} | {d.image_id for d in drafts}
    touched.discard(None)
    if touched:
        images_service.refresh_orphaned_images(conn, touched)
    return PresetSaveResult(presets=read_presets(conn, story), issues=issues)
