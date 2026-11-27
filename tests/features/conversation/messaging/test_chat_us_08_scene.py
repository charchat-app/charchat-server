"""CHAT-US-08 장면 유지·변화: 정상 완료 턴의 판단 → 장면 저장 → 다음 요청 반영(KAN-22)."""

import pytest
from messaging_helpers import (
    GENERATIONS,
    PLAYER,
    send,
    session_characters,
    start_session,
    user_part,
)

from app.features.conversation.messaging.providers import ProviderError
from app.features.conversation.scene import judge as scene_judge
from app.features.conversation.scene import repository as scene_repository

PARK = (
    '{"changed": true, "place": "공원 벤치", "days_passed": 1, '
    '"time": {"hour": 12, "minute": null}, '
    '"evidence": "다음 날 점심, 공원 벤치", "evidence_from": "user"}'
)


class ScriptedJudge:
    """판단 호출을 기록하고 정해진 판단 원문을 돌려준다."""

    def __init__(self, answers=None, error=None):
        self.answers = list(answers or [])
        self.error = error
        self.requests = []

    def stream(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        yield self.answers.pop(0) if self.answers else '{"changed": false}'


@pytest.fixture
def judge():
    original = scene_judge.get_judge_provider
    provider = ScriptedJudge()
    scene_judge.get_judge_provider = lambda: provider
    yield provider
    scene_judge.get_judge_provider = original


def scene_row(db, session_id):
    return db.execute(
        """SELECT date_mode, year, month, day, hour, minute, place, reference_event, elapsed_day
           FROM conversation_scene_states WHERE session_id = %s""",
        (session_id,),
    ).fetchone()


def changes(db, session_id):
    return db.execute(
        "SELECT * FROM conversation_scene_changes WHERE session_id = %s ORDER BY id", (session_id,)
    ).fetchall()


def generation_status(client, response):
    return client.get(f"{GENERATIONS}/{response.json()['id']}", headers=PLAYER).json()["status"]


def test_chat_us_08_ac01_no_change_keeps_scene_and_records_nothing(client, db, session, judge):
    before = scene_row(db, session)
    response = send(client, session, [user_part("여기 바람이 차네요.")])
    assert generation_status(client, response) == "completed"
    assert len(judge.requests) == 1
    assert scene_row(db, session) == before
    assert changes(db, session) == []


def test_chat_us_08_ac02_explicit_move_is_saved_with_evidence_and_used_next_turn(
    client, db, session, judge, recorder
):
    judge.answers = [PARK]
    parts = [
        user_part("가자."),
        {"speaker_kind": "narrator", "character_id": None, "body": "*다음 날 점심, 공원 벤치.*"},
    ]
    first = send(client, session, parts, request_id="turn-0001")
    assert generation_status(client, first) == "completed"
    # 판단 요청은 저장된 장면, 이번 턴 입력(나레이터 포함), 이번 응답을 본다.
    (judge_request,) = judge.requests
    content = judge_request.messages[1]["content"]
    for expected in (
        "장소: 경찰서 옥상",
        "[나레이터] *다음 날 점심, 공원 벤치.*",
        "@도경: 응답 1번",
    ):
        assert expected in content
    assert judge_request.model == "gpt-6-luna"

    row = scene_row(db, session)
    assert (row["month"], row["day"], row["hour"], row["minute"], row["place"]) == (
        11,
        23,
        12,
        None,
        "공원 벤치",
    )
    (change,) = changes(db, session)
    generation = db.execute(
        "SELECT user_message_id, assistant_message_id FROM conversation_generations WHERE id = %s",
        (first.json()["id"],),
    ).fetchone()
    assert change["evidence_from"] == "user"
    assert change["evidence_message_id"] == generation["user_message_id"]
    assert change["assistant_message_id"] == generation["assistant_message_id"]
    assert change["evidence"] == "다음 날 점심, 공원 벤치"
    assert change["before_state"]["scene"]["place"] == "경찰서 옥상"
    assert change["after_state"]["scene"]["place"] == "공원 벤치"

    # 다음 요청: 장소 언급이 없어도 현재 장면은 공원이고, 최신 입력 바로 앞에 붙는다.
    second = send(client, session, [user_part("배고프다.")], request_id="turn-0002")
    assert generation_status(client, second) == "completed"
    messages = recorder.requests[-1].messages
    assert "[현재 장면]" not in messages[1]["content"]
    assert messages[-1]["content"].startswith(
        "[현재 장면]\n장소: 공원 벤치\n날짜: 2026년 11월 23일"
    )
    assert "시각: 12시" in messages[-1]["content"]
    assert messages[-1]["content"].endswith("[이용자 입력]\n[민서] 배고프다.")


def test_chat_us_08_ac03_saved_scene_survives_reopen(client, db, session, judge, recorder):
    judge.answers = [PARK]
    send(client, session, [user_part("공원으로.")], request_id="turn-0001")
    # 다시 열기: 세션·대화 기록을 새로 읽어도 서버에 저장된 현재 장면을 쓴다.
    assert client.get(f"/api/v1/conversation/sessions/{session}", headers=PLAYER).status_code == 200
    send(client, session, [user_part("계속.")], request_id="turn-0002")
    assert "장소: 공원 벤치" in recorder.requests[-1].messages[-1]["content"]
    assert "경찰서 옥상" not in recorder.requests[-1].messages[-1]["content"]


def test_chat_us_08_failed_turn_does_not_judge_or_change_scene(
    client, db, session, judge, recorder
):
    recorder.chunks = ["@도경: 일부만", " 보였다"]
    recorder.error = ProviderError("STREAM_INTERRUPTED")
    recorder.error_after = 1
    judge.answers = [PARK]
    before = scene_row(db, session)
    response = send(client, session, [user_part("다음 날 공원으로.")])
    assert generation_status(client, response) == "failed_partial"
    assert judge.requests == []
    assert scene_row(db, session) == before
    assert changes(db, session) == []


def test_chat_us_08_judge_failure_keeps_reply_and_scene(client, db, session, judge):
    judge.error = RuntimeError("판단 호출 실패")
    before = scene_row(db, session)
    response = send(client, session, [user_part("다음 날 공원으로.")])
    assert generation_status(client, response) == "completed"
    assert scene_row(db, session) == before


def test_chat_us_08_apply_error_keeps_reply(client, db, session, judge, monkeypatch):
    judge.answers = [PARK]

    def broken(*args, **kwargs):
        raise RuntimeError("장면 저장 오류")

    monkeypatch.setattr(scene_repository, "update_scene", broken)
    before = scene_row(db, session)
    response = send(client, session, [user_part("다음 날 공원으로.")])
    assert generation_status(client, response) == "completed"
    assert scene_row(db, session) == before
    assert changes(db, session) == []


def test_chat_us_08_judge_disabled_skips_call(client, db, session, judge, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "chat_scene_judge_enabled", False)
    judge.answers = [PARK]
    send(client, session, [user_part("다음 날 공원으로.")])
    assert judge.requests == []
    assert scene_row(db, session)["place"] == "경찰서 옥상"


def test_chat_us_08_character_outfit_changes_only_that_character(client, db, session, judge):
    judge.answers = [
        '{"changed": true, "characters": [{"name": "도경", "clothing": "회색 셔츠", '
        '"inner_thought": null}], "evidence": "도경이 코트를 벗었다"}'
    ]
    send(client, session, [user_part("코트 좀 벗어요.")])
    rows = {
        row["name_snapshot"]: row
        for row in db.execute(
            """SELECT name_snapshot, clothing, inner_thought FROM conversation_character_states
               WHERE session_id = %s""",
            (session,),
        ).fetchall()
    }
    assert (rows["도경"]["clothing"], rows["도경"]["inner_thought"]) == ("회색 셔츠", "피곤하다")
    assert rows["Bea"]["clothing"] is None
    (change,) = changes(db, session)
    assert change["evidence_from"] == "assistant"
    assert scene_row(db, session)["place"] == "경찰서 옥상"


def test_chat_us_08_scene_change_stays_in_its_session(client, db, saved_story, session, judge):
    story, intro, _ = saved_story
    other = start_session(client, story, intro, headers={"X-User-Id": "1"})
    judge.answers = [PARK]
    send(client, session, [user_part("공원으로.")])
    assert scene_row(db, session)["place"] == "공원 벤치"
    assert scene_row(db, other)["place"] == "경찰서 옥상"
    assert changes(db, other) == []


def test_chat_us_08_retry_after_failure_applies_change_once(client, db, session, judge, recorder):
    recorder.error = ProviderError("PROVIDER_ERROR")
    failed = send(client, session, [user_part("다음 날 공원으로.")])
    assert generation_status(client, failed) == "failed"
    recorder.error = None
    judge.answers = [PARK]
    retried = client.post(
        f"{GENERATIONS}/{failed.json()['id']}/retry",
        headers=PLAYER,
        json={"request_id": "retry-0001"},
    )
    assert retried.status_code == 202, retried.text
    assert generation_status(client, retried) == "completed"
    assert len(changes(db, session)) == 1
    assert scene_row(db, session)["day"] == 23


def test_chat_us_08_judge_sees_session_character_names(client, db, session, judge):
    chars = session_characters(client, session)
    assert set(chars) == {"도경", "Bea"}
    send(client, session, [user_part("안녕.")])
    assert judge.requests[0].character_names == ["도경", "Bea"]
