"""CREATE-US-07-AC10 스토리 소프트 삭제 유스케이스.

공개 범위(`save_state`)와 별개로 `stories.deleted_at`만 기록한다. 스토리 행과 자식·세션·이미지
연결은 그대로 보존하고, 삭제한 스토리를 되살리는 경로는 두지 않는다.
HTTP 객체를 다루지 않으며 트랜잭션도 열지 않는다. 호출자의 트랜잭션 안에서 그대로 합성할 수 있다.
"""

from dataclasses import dataclass
from datetime import datetime

from psycopg import Connection

from app.features.creation.story_basic.service import StoryNotFoundError


@dataclass(frozen=True)
class SoftDeleteResult:
    story_id: int
    deleted_at: datetime


# 소유자 확인·미삭제 확인·삭제 기록을 한 문장으로 처리해 확인과 변경 사이에 틈이 없다.
# 같은 스토리를 다루는 저장 서비스의 행 잠금(FOR UPDATE)과도 UPDATE 자체의 행 잠금으로 직렬화된다.
# saved_at 갱신과 복구·물리 삭제 금지는 stories_lifecycle_guard 트리거가 DB에서도 지킨다.
_SOFT_DELETE_SQL = """
    UPDATE stories SET deleted_at = clock_timestamp()
    WHERE id = %s AND owner_id = %s AND deleted_at IS NULL
    RETURNING id, deleted_at
"""


def soft_delete_story(conn: Connection, owner_id: int, story_id: int) -> SoftDeleteResult:
    """본인의 스토리를 소프트 삭제한다 (draft·private·public 모두).

    없는 스토리·타인 스토리·이미 삭제된 스토리는 구분하지 않고 같은 `StoryNotFoundError`로
    응답해 존재 여부를 드러내지 않는다. 재요청해도 최초 삭제 시각은 유지된다.
    """
    row = conn.execute(_SOFT_DELETE_SQL, (story_id, owner_id)).fetchone()
    if row is None:
        raise StoryNotFoundError(f"스토리 {story_id}를 찾을 수 없습니다.")
    return SoftDeleteResult(story_id=row["id"], deleted_at=row["deleted_at"])
