"""CREATE-US-09 실제 반영된 스토리의 분류 필요 상태를 관리한다."""

from app.features.creation.category_classification import repository
from app.features.creation.category_classification.worker import MAX_FIRST_ATTEMPTS


def committed_inputs(connection, story_id):
    return repository.read_inputs(connection, story_id)


def mark_after_basic_change(connection, story_id, previous):
    """확정 분류 입력이 실제로 바뀐 경우에만 분류 필요 표시와 작업 기록을 갱신한다.

    호출 측은 스토리 행을 잠근 같은 트랜잭션 안에서 부른다. 재분류 시도 기록을 지워 이 변경이
    시도 예산 3회를 새로 쓰게 하고, 진행 중인 이전 시도의 예약 토큰도 무효로 만든다(ABA 포함).
    변화 없는 재저장·제목만 변경·공개 범위 변경·임시 저장은 여기 오지 않거나 비교에서 걸러진다.
    """
    if previous != committed_inputs(connection, story_id):
        repository.mark_needed(connection, story_id)
        repository.reopen_first_for_edit(connection, story_id, MAX_FIRST_ATTEMPTS)
        repository.reset_reclassification(connection, story_id)


def mark_after_registration(connection, story_id, *, previous_state, current_state) -> bool:
    """미등록→비공개/공개 최초 등록이면 분류 필요로 표시하고 True를 돌려준다.

    호출 측은 스토리 행을 잠근 같은 트랜잭션 안에서 부르고, 커밋 뒤에만 최초 분류를 맡긴다.
    """
    if previous_state == "draft" and current_state in {"private", "public"}:
        repository.mark_needed(connection, story_id)
        repository.enqueue_first(connection, story_id)
        return True
    return False
