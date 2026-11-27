"""CHAT-US-08 이용자가 현재 장면을 직접 보고 고친다(KAN-22, 11-27 결정 C).

AI가 매 턴 판단해 바꾸고, 이용자도 직접 고칠 수 있다. 고친 값은 다음 전송의 [현재 장면]에 쓰이고
AI 판단은 그 위에서 이어 바꾼다. 세션 주인은 PLAYER(2), 스토리 제작자는 AUTHOR(1)다.
"""

import pytest
from scene_helpers import (
    AUTHOR,
    GENERATIONS,
    PLAYER,
    STRANGER,
    changes,
    editable,
    get_scene,
    put_scene,
    scene_snapshot,
    send,
    start_session,
)

from app.features.conversation.messaging import providers

PARK_EDIT = {
    "era": None,
    "year": 2026,
    "month": 11,
    "day": 28,
    "reference_event": None,
    "elapsed_day": None,
    "hour": 12,
    "minute": 0,
    "place": "공원",
}


def character_ids(client, session_id, headers=PLAYER) -> dict[str, int]:
    scene = get_scene(client, session_id, headers=headers).json()
    return {row["name"]: row["id"] for row in scene["characters"]}


def park_edit(client, session_id) -> dict:
    ids = character_ids(client, session_id)
    return {
        **PARK_EDIT,
        "characters": [{"id": ids["도경"], "clothing": "회색 셔츠", "inner_thought": None}],
    }


def set_date_mode(db, session_id, mode, **values):
    """세션 날짜 방식을 바꾸고(테스트 준비용) 날짜 칸을 방식에 맞게 채운다."""
    date = {
        "era": None,
        "year": None,
        "month": None,
        "day": None,
        "reference_event": None,
        "elapsed_day": None,
    }
    date.update(values)
    db.execute(
        """UPDATE conversation_scene_states
           SET date_mode = %(mode)s, era = %(era)s, year = %(year)s, month = %(month)s,
               day = %(day)s, reference_event = %(reference_event)s,
               elapsed_day = %(elapsed_day)s
           WHERE session_id = %(session)s""",
        {**date, "mode": mode, "session": session_id},
    )


# ---- 조회 ----


def test_chat_us_08_user_edit_get_returns_current_scene(client, session):
    response = get_scene(client, session)
    assert response.status_code == 200
    body = response.json()
    first, second = body.pop("characters")
    assert body == {
        "date_mode": "calendar",
        "era": None,
        "year": 2026,
        "month": 11,
        "day": 22,
        "reference_event": None,
        "elapsed_day": None,
        "hour": 21,
        "minute": 5,
        "place": "경찰서 옥상",
        "read_only": False,
    }
    # 세션 캐릭터 상태 순서(id), 이름은 세션 이름(name_snapshot).
    assert first["id"] < second["id"]
    assert {k: v for k, v in first.items() if k != "id"} == {
        "name": "도경",
        "clothing": "젖은 코트",
        "inner_thought": "피곤하다",
    }
    assert {k: v for k, v in second.items() if k != "id"} == {
        "name": "Bea",
        "clothing": None,
        "inner_thought": None,
    }


@pytest.mark.parametrize("intruder", [AUTHOR, STRANGER], ids=["story_author", "stranger"])
def test_chat_us_08_user_edit_get_other_account_is_404(client, session, intruder):
    response = get_scene(client, session, headers=intruder)
    assert response.status_code == 404
    assert response.json()["code"] == "CONVERSATION_SESSION_NOT_FOUND"
    for value in ("경찰서 옥상", "젖은 코트", "피곤하다", "2026"):
        assert value not in response.text


def test_chat_us_08_user_edit_unknown_session_is_404(client, session):
    for session_id in (session + 1000, 2**63):
        assert get_scene(client, session_id).status_code == 404
        response = put_scene(client, session_id, {**PARK_EDIT, "characters": []})
        assert response.status_code == 404
        assert response.json()["code"] == "CONVERSATION_SESSION_NOT_FOUND"


def test_chat_us_08_user_edit_deleted_story_is_read_only(client, db, saved_story, session):
    story, _ = saved_story
    db.execute("UPDATE stories SET deleted_at = clock_timestamp() WHERE id = %s", (story,))
    response = get_scene(client, session)
    assert response.status_code == 200
    assert response.json()["read_only"] is True
    assert response.json()["place"] == "경찰서 옥상"

    before = scene_snapshot(db, session)
    edited = put_scene(client, session, park_edit(client, session))
    assert edited.status_code == 409
    assert edited.json()["code"] == "CONVERSATION_READ_ONLY"
    assert scene_snapshot(db, session) == before


# ---- 수정 ----


def test_chat_us_08_user_edit_put_updates_scene_records_change_and_next_prompt(
    client, db, session, recorder
):
    ids = character_ids(client, session)
    response = put_scene(client, session, park_edit(client, session))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == get_scene(client, session).json()
    assert (body["date_mode"], body["year"], body["month"], body["day"]) == (
        "calendar",
        2026,
        11,
        28,
    )
    assert (body["hour"], body["minute"], body["place"]) == (12, 0, "공원")
    dokyung, bea = body["characters"]
    assert (dokyung["clothing"], dokyung["inner_thought"]) == ("회색 셔츠", None)
    # 목록에 없는 캐릭터는 그대로 둔다.
    assert (bea["id"], bea["clothing"], bea["inner_thought"]) == (ids["Bea"], None, None)

    row = db.execute(
        "SELECT * FROM conversation_scene_states WHERE session_id = %s", (session,)
    ).fetchone()
    assert (row["year"], row["month"], row["day"], row["hour"], row["minute"], row["place"]) == (
        2026,
        11,
        28,
        12,
        0,
        "공원",
    )
    (change,) = changes(db, session)
    assert change["source"] == "user_edit"
    assert (
        change["generation_id"],
        change["assistant_message_id"],
        change["evidence_message_id"],
        change["evidence_from"],
        change["evidence"],
    ) == (None, None, None, None, "")
    assert change["before_state"]["scene"]["place"] == "경찰서 옥상"
    assert change["before_state"]["scene"]["date_mode"] == "calendar"
    assert change["after_state"]["scene"] == {
        "date_mode": "calendar",
        **PARK_EDIT,
    }
    assert change["before_state"]["characters"][0] == {
        "id": ids["도경"],
        "name": "도경",
        "clothing": "젖은 코트",
        "inner_thought": "피곤하다",
    }
    assert change["after_state"]["characters"][0]["clothing"] == "회색 셔츠"
    assert change["after_state"]["characters"][0]["inner_thought"] is None

    # 다음 전송의 [현재 장면]에 고친 값이 들어간다(전송 때 DB에서 다시 읽는다).
    sent = send(client, session, "배고프다.")
    assert client.get(f"{GENERATIONS}/{sent.json()['id']}", headers=PLAYER).json()["status"] == (
        "completed"
    )
    content = recorder.requests[-1].messages[-1]["content"]
    scene_block = content.split("[현재 장면]\n")[1]
    assert scene_block.startswith("장소: 공원\n날짜: 2026년 11월 28일\n시각: 12:00")
    assert "도경 착장: 회색 셔츠" in scene_block
    assert "도경 속마음" not in scene_block
    assert "경찰서 옥상" not in content


def test_chat_us_08_user_edit_same_values_record_nothing(client, db, session):
    current = get_scene(client, session).json()
    body = editable(current)
    # 앞뒤 공백은 지우고 비교한다.
    body["place"] = "  경찰서 옥상 "
    body["characters"][0]["clothing"] = " 젖은 코트"
    before = scene_snapshot(db, session)
    response = put_scene(client, session, body)
    assert response.status_code == 200
    assert response.json() == current
    assert scene_snapshot(db, session) == before
    assert changes(db, session) == []

    # 캐릭터를 빼고 보내도 그 캐릭터는 그대로이고 기록도 없다.
    response = put_scene(client, session, {**body, "characters": []})
    assert response.status_code == 200
    assert scene_snapshot(db, session) == before


def test_chat_us_08_user_edit_null_and_blank_clear_values(client, db, session):
    ids = character_ids(client, session)
    body = {
        **{key: None for key in PARK_EDIT},
        "place": "   ",
        "characters": [{"id": ids["도경"], "clothing": "", "inner_thought": None}],
    }
    response = put_scene(client, session, body)
    assert response.status_code == 200, response.text
    scene = response.json()
    assert scene["date_mode"] == "calendar"  # 날짜 방식은 바뀌지 않는다
    assert [scene[key] for key in PARK_EDIT] == [None] * len(PARK_EDIT)
    assert scene["characters"][0]["clothing"] is None
    assert len(changes(db, session)) == 1


@pytest.mark.parametrize(
    ("mode", "date"),
    [
        ("world", {"era": "신력", "year": 3, "month": 2, "day": 28}),
        ("elapsed", {"reference_event": "탈출", "elapsed_day": 9999}),
        ("elapsed", {"reference_event": "탈출", "elapsed_day": 10000}),  # 대화 중 상한 아님
        ("elapsed", {}),
        (None, {}),
    ],
    ids=["world", "elapsed", "elapsed_over_9999", "elapsed_unspecified", "no_date_mode"],
)
def test_chat_us_08_user_edit_valid_date_per_mode(client, db, session, mode, date):
    set_date_mode(db, session, mode)
    body = {
        **{key: None for key in PARK_EDIT},
        "hour": 0,
        "minute": 59,
        "place": "가" * 30,
        "characters": [],
    }
    body.update(date)
    response = put_scene(client, session, body)
    assert response.status_code == 200, response.text
    scene = response.json()
    assert scene["date_mode"] == mode
    assert {key: scene[key] for key in body if key != "characters"} == {
        key: value for key, value in body.items() if key != "characters"
    }


CALENDAR = {**PARK_EDIT}
WORLD = {**PARK_EDIT, "era": "신력"}
ELAPSED = {**PARK_EDIT, "year": None, "month": None, "day": None}
ELAPSED.update(reference_event="탈출", elapsed_day=3)
NO_DATE = {**ELAPSED, "reference_event": None, "elapsed_day": None}


@pytest.mark.parametrize(
    ("mode", "body", "character", "issues"),
    [
        ("calendar", {**CALENDAR, "day": None}, None, [("day", "SCENE_DATE_INCOMPLETE")]),
        (
            "calendar",
            {**CALENDAR, "year": None, "month": None},
            None,
            [("year", "SCENE_DATE_INCOMPLETE"), ("month", "SCENE_DATE_INCOMPLETE")],
        ),
        ("world", {**WORLD, "era": None}, None, [("era", "SCENE_DATE_INCOMPLETE")]),
        ("world", {**WORLD, "era": "  "}, None, [("era", "SCENE_DATE_INCOMPLETE")]),
        (
            "elapsed",
            {**ELAPSED, "elapsed_day": None},
            None,
            [("elapsed_day", "SCENE_DATE_INCOMPLETE")],
        ),
        ("calendar", {**CALENDAR, "era": "신력"}, None, [("era", "SCENE_FIELD_NOT_USED")]),
        (
            "calendar",
            {**CALENDAR, "elapsed_day": 3},
            None,
            [("elapsed_day", "SCENE_FIELD_NOT_USED")],
        ),
        ("elapsed", {**ELAPSED, "year": 2026}, None, [("year", "SCENE_FIELD_NOT_USED")]),
        (None, {**NO_DATE, "day": 1}, None, [("day", "SCENE_FIELD_NOT_USED")]),
        (
            "calendar",
            {**CALENDAR, "month": 2, "day": 29},
            None,
            [("day", "SCENE_VALUE_OUT_OF_RANGE")],
        ),
        ("calendar", {**CALENDAR, "day": 0}, None, [("day", "SCENE_VALUE_OUT_OF_RANGE")]),
        ("calendar", {**CALENDAR, "month": 13}, None, [("month", "SCENE_VALUE_OUT_OF_RANGE")]),
        (
            "calendar",
            {**CALENDAR, "year": 10000},
            None,
            [("year", "SCENE_VALUE_OUT_OF_RANGE")],
        ),
        ("calendar", {**CALENDAR, "year": 0}, None, [("year", "SCENE_VALUE_OUT_OF_RANGE")]),
        (
            "elapsed",
            {**ELAPSED, "elapsed_day": 2**31},
            None,
            [("elapsed_day", "SCENE_VALUE_OUT_OF_RANGE")],
        ),
        ("calendar", {**CALENDAR, "minute": None}, None, [("minute", "SCENE_TIME_INCOMPLETE")]),
        ("calendar", {**CALENDAR, "hour": None}, None, [("hour", "SCENE_TIME_INCOMPLETE")]),
        ("calendar", {**CALENDAR, "hour": 24}, None, [("hour", "SCENE_VALUE_OUT_OF_RANGE")]),
        ("calendar", {**CALENDAR, "minute": 60}, None, [("minute", "SCENE_VALUE_OUT_OF_RANGE")]),
        ("calendar", {**CALENDAR, "hour": -1}, None, [("hour", "SCENE_VALUE_OUT_OF_RANGE")]),
        ("calendar", {**CALENDAR, "place": "가" * 31}, None, [("place", "SCENE_TEXT_TOO_LONG")]),
        ("world", {**WORLD, "era": "가" * 11}, None, [("era", "SCENE_TEXT_TOO_LONG")]),
        (
            "elapsed",
            {**ELAPSED, "reference_event": "가" * 11},
            None,
            [("reference_event", "SCENE_TEXT_TOO_LONG")],
        ),
        (
            "calendar",
            CALENDAR,
            {"clothing": "가" * 301, "inner_thought": None},
            [("characters[0].clothing", "SCENE_TEXT_TOO_LONG")],
        ),
        (
            "calendar",
            CALENDAR,
            {"clothing": None, "inner_thought": "가" * 101},
            [("characters[0].inner_thought", "SCENE_TEXT_TOO_LONG")],
        ),
        ("calendar", {**CALENDAR, "place": "공\x00원"}, None, [("place", "SCENE_TEXT_INVALID")]),
        (
            "calendar",
            CALENDAR,
            {"clothing": "셔츠\x00", "inner_thought": None},
            [("characters[0].clothing", "SCENE_TEXT_INVALID")],
        ),
        ("calendar", {**CALENDAR, "place": "공\ud800원"}, None, [("place", "SCENE_TEXT_INVALID")]),
    ],
)
def test_chat_us_08_user_edit_invalid_values_are_rejected_without_change(
    client, db, session, mode, body, character, issues
):
    if mode != "calendar":
        set_date_mode(db, session, mode)
    characters = []
    if character is not None:
        characters = [{"id": character_ids(client, session)["도경"], **character}]
    before = scene_snapshot(db, session)
    response = put_scene(client, session, {**body, "characters": characters})
    assert response.status_code == 422, response.text
    problem = response.json()
    assert problem["code"] == "CONVERSATION_SCENE_INVALID"
    assert [(issue["field"], issue["code"]) for issue in problem["issues"]] == issues
    assert scene_snapshot(db, session) == before


@pytest.mark.parametrize(
    "change",
    [
        {"year": "2026"},
        {"hour": True},
        {"minute": 1.0},
        {"place": 3},
        {"characters": [{"id": "1", "clothing": None, "inner_thought": None}]},
        {"characters": [{"id": 1, "clothing": None}]},
        {"characters": None},
        {"date_mode": "world"},
    ],
    ids=[
        "string_year",
        "bool_hour",
        "float_minute",
        "number_place",
        "string_character_id",
        "missing_character_key",
        "null_characters",
        "date_mode_not_editable",
    ],
)
def test_chat_us_08_user_edit_request_shape_is_strict(client, db, session, change):
    before = scene_snapshot(db, session)
    response = put_scene(client, session, {**PARK_EDIT, "characters": [], **change})
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert scene_snapshot(db, session) == before


@pytest.mark.parametrize("missing", [*PARK_EDIT, "characters"])
def test_chat_us_08_user_edit_every_key_is_required(client, db, session, missing):
    body = {**PARK_EDIT, "characters": []}
    del body[missing]
    before = scene_snapshot(db, session)
    response = put_scene(client, session, body)
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert scene_snapshot(db, session) == before


def test_chat_us_08_user_edit_character_must_belong_to_session(client, db, saved_story, session):
    story, intro = saved_story
    # 같은 스토리의 다른 세션(같은 계정이 다시 시작하면 기존 세션을 이어 쓰므로 제작자 세션).
    other = start_session(client, story, intro, headers=AUTHOR)
    assert other != session
    other_id = character_ids(client, other, headers=AUTHOR)["도경"]
    own_id = character_ids(client, session)["도경"]
    before, other_before = scene_snapshot(db, session), scene_snapshot(db, other)
    for characters, expected in (
        ([{"id": other_id}], [("characters[0].id", "SCENE_CHARACTER_NOT_FOUND")]),
        ([{"id": 2**63}], [("characters[0].id", "SCENE_CHARACTER_NOT_FOUND")]),
        (
            [{"id": own_id}, {"id": own_id}],
            [("characters[1].id", "SCENE_CHARACTER_DUPLICATE")],
        ),
    ):
        body = {
            **PARK_EDIT,
            "characters": [
                {**c, "clothing": "회색 셔츠", "inner_thought": None} for c in characters
            ],
        }
        response = put_scene(client, session, body)
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "CONVERSATION_SCENE_CHARACTER_INVALID"
        assert [(i["field"], i["code"]) for i in response.json()["issues"]] == expected
    assert scene_snapshot(db, session) == before
    assert scene_snapshot(db, other) == other_before


@pytest.mark.parametrize("intruder", [AUTHOR, STRANGER], ids=["story_author", "stranger"])
def test_chat_us_08_user_edit_other_account_put_is_404_without_change(
    client, db, session, intruder
):
    body = park_edit(client, session)
    before = scene_snapshot(db, session)
    response = put_scene(client, session, body, headers=intruder)
    assert response.status_code == 404
    assert response.json()["code"] == "CONVERSATION_SESSION_NOT_FOUND"
    assert "경찰서 옥상" not in response.text
    assert scene_snapshot(db, session) == before


# ---- 진행 중 생성 ----


def test_chat_us_08_user_edit_blocked_while_generation_pending(
    client, db, session, recorder, deferred
):
    body = park_edit(client, session)
    sent = send(client, session, "아직 시작 전인 요청")
    assert sent.json()["status"] == "pending"
    before = scene_snapshot(db, session)
    response = put_scene(client, session, body)
    assert response.status_code == 409
    assert response.json()["code"] == "GENERATION_IN_PROGRESS"
    assert scene_snapshot(db, session) == before

    # 응답이 끝나면 고칠 수 있다.
    deferred.run()
    assert put_scene(client, session, body).status_code == 200
    assert [row["source"] for row in changes(db, session)] == ["user_edit"]


def test_chat_us_08_user_edit_blocked_while_generation_streaming(
    client, db, session, deferred, monkeypatch
):
    body = park_edit(client, session)
    seen = {}

    class EditMidStream:
        def stream(self, request):
            yield "@도경: 응답 "
            seen["before"] = scene_snapshot(db, session)
            seen["response"] = put_scene(client, session, body)
            yield "끝\n@나레이터: 끝"

    monkeypatch.setattr(providers, "get_provider", lambda: EditMidStream())
    send(client, session, "가자")
    deferred.run()
    assert seen["response"].status_code == 409
    assert seen["response"].json()["code"] == "GENERATION_IN_PROGRESS"
    assert scene_snapshot(db, session) == seen["before"]


def age_generations(db, session_id):
    db.execute(
        """UPDATE conversation_generations SET updated_at = clock_timestamp() - interval '1 day'
           WHERE session_id = %s""",
        (session_id,),
    )


def generation_state(db, generation_id):
    row = db.execute(
        "SELECT status, error_code FROM conversation_generations WHERE id = %s", (generation_id,)
    ).fetchone()
    return row["status"], row["error_code"]


def test_chat_us_08_user_edit_stale_generation_does_not_block(
    client, db, session, recorder, judge, deferred
):
    """멈춘 생성은 수정 전에 실패로 정리한다. 작업이 늦게 돌아도 고친 장면을 덮어쓰지 않는다."""
    body = park_edit(client, session)
    sent = send(client, session, "멈춘 요청")
    assert sent.json()["status"] == "pending"
    age_generations(db, session)
    response = put_scene(client, session, body)
    assert response.status_code == 200, response.text
    assert generation_state(db, sent.json()["id"]) == ("failed", "GENERATION_STALE")
    edited = scene_snapshot(db, session)

    # 늦게 집힌 작업: 이미 정리된 생성이라 실행하지 않고, 장면 판단도 적용하지 않는다.
    judge.answers = [MOVE_TO_CAFE]
    deferred.run()
    assert recorder.requests == []
    assert judge.requests == []
    assert generation_state(db, sent.json()["id"]) == ("failed", "GENERATION_STALE")
    assert scene_snapshot(db, session) == edited
    assert [row["source"] for row in changes(db, session)] == ["user_edit"]


def test_chat_us_08_user_edit_stale_streaming_generation_cannot_overwrite_edit(
    client, db, session, judge, deferred, monkeypatch
):
    """응답 중 멈춘 것으로 보인 생성(작업은 살아 있음)이 수정 뒤에 완료 저장하지 못한다."""
    body = park_edit(client, session)
    seen = {}

    class EditAfterStall:
        def stream(self, request):
            yield "@도경: 응답 "
            age_generations(db, session)
            seen["response"] = put_scene(client, session, body)
            seen["edited"] = scene_snapshot(db, session)
            yield "끝\n@나레이터: 다음 날 아침 공원 앞 카페로 갔다"

    monkeypatch.setattr(providers, "get_provider", lambda: EditAfterStall())
    judge.answers = [MOVE_TO_CAFE]
    sent = send(client, session, "다음 날 아침 공원 앞 카페로 갔다.")
    deferred.run()
    assert seen["response"].status_code == 200, seen["response"].text
    assert generation_state(db, sent.json()["id"]) == ("failed_partial", "GENERATION_STALE")
    assert judge.requests == []
    assert scene_snapshot(db, session) == seen["edited"]
    assert [row["source"] for row in changes(db, session)] == ["user_edit"]


# ---- 이용자 수정 뒤 AI 판단 ----

MOVE_TO_CAFE = (
    '{"changed": true, "place": "공원 앞 카페", "days_passed": 1, '
    '"time": {"hour": 9, "minute": 30}, '
    '"evidence": "다음 날 아침 공원 앞 카페로 갔다", "evidence_from": "user"}'
)
USER_EDITED_NOTE = "※ 이용자가 방금 이 장면을 직접 고쳤다. 앞 대화와 달라도 이 장면에서 이어 간다."


def test_chat_us_08_user_edit_then_judge_continues_from_edited_scene(
    client, db, session, recorder, judge
):
    assert put_scene(client, session, park_edit(client, session)).status_code == 200
    judge.answers = [MOVE_TO_CAFE]
    sent = send(client, session, "다음 날 아침 공원 앞 카페로 갔다.")
    assert client.get(f"{GENERATIONS}/{sent.json()['id']}", headers=PLAYER).json()["status"] == (
        "completed"
    )
    # 판단 요청도 고친 장면을 지금 장면으로 보고, 직접 고쳤다는 알림을 받는다.
    judged_scene = judge.requests[0].messages[1]["content"]
    assert "장소: 공원\n" in judged_scene
    assert USER_EDITED_NOTE in judged_scene
    scene = get_scene(client, session).json()
    assert (scene["month"], scene["day"], scene["hour"], scene["minute"], scene["place"]) == (
        11,
        29,
        9,
        30,
        "공원 앞 카페",
    )
    assert scene["characters"][0]["clothing"] == "회색 셔츠"
    edit, judged = changes(db, session)
    assert (edit["source"], judged["source"]) == ("user_edit", "judge")
    assert judged["generation_id"] == sent.json()["id"]
    assert judged["before_state"] == edit["after_state"]


def test_chat_us_08_user_edit_note_only_on_first_send_after_edit(client, db, session, recorder):
    def scene_block():
        return recorder.requests[-1].messages[-1]["content"].split("[현재 장면]\n")[1]

    # 고친 적 없음: 알림 없음.
    send(client, session, "첫 대화", request_id="turn-0001")
    assert USER_EDITED_NOTE not in scene_block()

    assert put_scene(client, session, park_edit(client, session)).status_code == 200
    send(client, session, "고친 뒤 첫 전송", request_id="turn-0002")
    assert scene_block().split("\n\n[이용자 입력]")[0].endswith("\n" + USER_EDITED_NOTE)
    assert "장소: 공원" in scene_block()
    assert all(USER_EDITED_NOTE not in m["content"] for m in recorder.requests[-1].messages[:-1])

    # 고친 장면으로 한 턴이 완료된 뒤에는 알리지 않는다.
    send(client, session, "다음 전송", request_id="turn-0003")
    assert USER_EDITED_NOTE not in scene_block()
    assert "장소: 공원" in scene_block()


def test_chat_us_08_user_edit_note_kept_until_a_turn_completes(client, db, session, monkeypatch):
    """고친 뒤 첫 턴이 실패하면 다시 시도한 요청에도 알림이 남는다(완료 턴이 아직 없으므로)."""
    requests = []

    class FailOnce:
        def stream(self, request):
            requests.append(request)
            if len(requests) == 1:
                raise providers.ProviderError("STREAM_INTERRUPTED")
            yield "@도경: 응답"

    monkeypatch.setattr(providers, "get_provider", lambda: FailOnce())
    assert put_scene(client, session, park_edit(client, session)).status_code == 200
    failed = send(client, session, "고친 뒤 첫 전송").json()
    retried = client.post(
        f"{GENERATIONS}/{failed['id']}/retry", json={"request_id": "retry-0001"}, headers=PLAYER
    )
    assert retried.status_code in (200, 201, 202), retried.text
    assert [USER_EDITED_NOTE in r.messages[-1]["content"] for r in requests] == [True, True]


def test_chat_us_08_user_edit_reports_character_and_scene_issues_together(client, db, session):
    before = scene_snapshot(db, session)
    body = {
        **PARK_EDIT,
        "day": 31,
        "month": 2,
        "characters": [{"id": 2**40, "clothing": "가" * 301, "inner_thought": None}],
    }
    response = put_scene(client, session, body)
    assert response.status_code == 422
    assert response.json()["code"] == "CONVERSATION_SCENE_CHARACTER_INVALID"
    assert [(i["field"], i["code"]) for i in response.json()["issues"]] == [
        ("day", "SCENE_VALUE_OUT_OF_RANGE"),
        ("characters[0].clothing", "SCENE_TEXT_TOO_LONG"),
        ("characters[0].id", "SCENE_CHARACTER_NOT_FOUND"),
    ]
    assert scene_snapshot(db, session) == before


def test_chat_us_08_user_edit_characters_list_is_bounded(client, db, session):
    own = character_ids(client, session)["도경"]
    row = {"id": own, "clothing": None, "inner_thought": None}
    before = scene_snapshot(db, session)
    response = put_scene(client, session, {**PARK_EDIT, "characters": [row] * 101})
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert scene_snapshot(db, session) == before
    # 100개까지는 형식 검사를 지나 중복 ID로 거절된다.
    response = put_scene(client, session, {**PARK_EDIT, "characters": [row] * 100})
    assert response.json()["code"] == "CONVERSATION_SCENE_CHARACTER_INVALID"
