"""CREATE-US-09 실제 반영된 스토리의 분류 필요 상태를 관리한다."""

from app.features.creation.category_classification import repository


def committed_inputs(connection, story_id):
    return repository.read_inputs(connection, story_id)


def mark_after_basic_change(connection, story_id, previous):
    if previous != committed_inputs(connection, story_id):
        repository.mark_needed(connection, story_id)


def mark_after_registration(connection, story_id, *, previous_state, current_state):
    if previous_state == "draft" and current_state in {"private", "public"}:
        repository.mark_needed(connection, story_id)
