"""CREATE-US-09-AC06: 카테고리 분류 대기·실행 중·실패는 공개 스토리 직접 진입과 대화를 막지 않는다.

외부 모델 호출 없이(가짜 공급자·인라인 생성) 실제 API와 PostgreSQL로 확인한다.
비공개·삭제 스토리의 일반 접근 경계는 기존 테스트가 담당한다:
- tests/features/conversation/starter/test_story.py::test_story_access
- tests/features/conversation/starter/test_sessions.py
  ::test_identical_start_retry_requires_current_story_access
- tests/features/conversation/resume/test_resume_sessions.py
  ::test_chat_us_03_ac01_ac02_follows_current_room_access_policy
- tests/features/conversation/messaging/test_messaging_send.py
  ::test_chat_us_02_deleted_story_session_is_read_only
"""

import pytest
from resume_helpers import PLAYER, SESSIONS, STORIES, lookup, send, start

# 상태별 실제 최초 분류 작업 기록(스키마 CHECK 범위 안의 값).
LEDGER = {
    "pending": (0, None),
    "running": (1, None),
    "failed": (3, "temporary"),
}


def _classification_state(db, story_id):
    job = db.execute(
        """SELECT status, attempts, failure_reason, created_at, updated_at
           FROM story_first_classification_jobs WHERE story_id = %s""",
        (story_id,),
    ).fetchone()
    flag = db.execute(
        "SELECT category_classification_needed FROM stories WHERE id = %s", (story_id,)
    ).fetchone()["category_classification_needed"]
    links = db.execute(
        "SELECT count(*) AS n FROM story_categories WHERE story_id = %s", (story_id,)
    ).fetchone()["n"]
    return job, flag, links


@pytest.mark.parametrize("status", sorted(LEDGER))
def test_create_us_09_ac06_unclassified_public_story_is_playable_by_nonowner(
    client, db, story, status
):
    story_id, intro, preset = story
    attempts, reason = LEDGER[status]
    db.execute(
        "UPDATE stories SET category_classification_needed = true WHERE id = %s", (story_id,)
    )
    db.execute(
        """INSERT INTO story_first_classification_jobs
               (story_id, status, attempts, failure_reason)
           VALUES (%s, %s, %s, %s)""",
        (story_id, status, attempts, reason),
    )
    before = _classification_state(db, story_id)
    job, flag, links = before
    assert (job["status"], flag, links) == (status, True, 0)

    # 직접 상세: 분류 결과가 없어도 비소유자에게 공개 스토리를 보여 준다.
    detail = client.get(f"{STORIES}/{story_id}", headers=PLAYER)
    assert detail.status_code == 200, detail.text
    assert detail.json()["id"] == story_id
    assert detail.json()["existing_session_id"] is None

    # 시작.
    session = start(client, story)

    # 다시 열기·이어하기: 같은 시작 재요청, 상세의 기존 세션, 목록·대화방 조회.
    assert start(client, story) == session
    detail = client.get(f"{STORIES}/{story_id}", headers=PLAYER)
    assert detail.json()["existing_session_id"] == session
    listed = client.get(SESSIONS, headers=PLAYER)
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()["sessions"]] == [session]
    room = client.get(f"{SESSIONS}/{session}", headers=PLAYER)
    assert room.status_code == 200
    assert room.json()["read_only"] is False

    # 메시지 전송이 접수되고 (인라인 가짜 생성으로) 완료된다.
    sent = send(client, session, "분류 전에도 말 걸기", request_id="ac06-send-1")
    assert sent.status_code == 202, sent.text
    found = lookup(client, session, "ac06-send-1")
    assert found.status_code == 200
    [generation] = found.json()["generations"]
    assert generation["id"] == sent.json()["id"]
    assert generation["status"] == "completed"

    # 대화 흐름은 분류 작업 기록·분류 필요 표시·결과 연결을 바꾸지 않는다.
    assert _classification_state(db, story_id) == before
