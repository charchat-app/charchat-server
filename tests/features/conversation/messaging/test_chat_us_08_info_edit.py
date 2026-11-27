"""CHAT-US-08 마지막 AI 응답의 장면 정보(info) 이용자 수정(KAN-40 결정 C, KAN-22).

PUT /conversation/sessions/{session_id}/messages/{message_id}/info 는 그 응답의 생성 원문에서
info 블록만 바꾼다. 말풍선·청구·상태는 그대로이고, 다음 요청의 [현재 장면]과 턴 API current_scene은
같은 _current_scene이 바뀐 원문을 다시 읽어 만든다.
"""

import pytest
from messaging_helpers import (
    AUTHOR,
    GENERATIONS,
    PLAYER,
    SESSIONS,
    info_reply,
    scene_block,
    send,
    start_session,
    user_part,
)

from app.features.conversation.messaging import service, worker
from app.features.conversation.messaging.info import replace_info, scene_view, split_info
from app.features.conversation.messaging.parser import parse_assistant_text

STRANGER = {"X-User-Id": "3"}
BODY = "@도경: *고개를 끄덕인다.*\n\n가자.\n@나레이터: *비가 그친다.*"
BUBBLES = [("도경", "*고개를 끄덕인다.*\n\n가자."), (None, "*비가 그친다.*")]
PARK_INFO = (
    "날짜: 2026년 11월 23일\n시간: 12시\n장소: 공원 벤치\n"
    "[도경] 착장: 회색 셔츠 | 속마음: -\n[Bea] 착장: 노란 우비 | 속마음: 배고프다\n"
    "[민서] 착장: 교복 | 속마음: 신난다"
)
# PARK_INFO 응답까지의 누적 장면(도경 속마음은 시작 값 '피곤하다'를 잇는다).
PARK_LINES = [
    "날짜: 2026년 11월 23일",
    "시간: 12시",
    "장소: 공원 벤치",
    "[도경] 착장: 회색 셔츠 | 속마음: 피곤하다",
    "[Bea] 착장: 노란 우비 | 속마음: 배고프다",
]
DATE_COLUMNS = ("era", "year", "month", "day", "reference_event", "elapsed_day")
PARK_VIEW = {
    "date": {
        "mode": "calendar",
        **dict.fromkeys(DATE_COLUMNS),
        "year": 2026,
        "month": 11,
        "day": 23,
    },
    "time": {"hour": 12, "minute": None},
    "place": "공원 벤치",
    "characters": [
        {"name": "도경", "clothing": "회색 셔츠", "inner_thought": "피곤하다"},
        {"name": "Bea", "clothing": "노란 우비", "inner_thought": "배고프다"},
    ],
}


def info_url(session_id, message_id):
    return f"{SESSIONS}/{session_id}/messages/{message_id}/info"


def turn(client, session_id, recorder, info, text, request_id, body=BODY, headers=PLAYER):
    recorder.chunks = info_reply(body, info)
    response = send(client, session_id, [user_part(text)], request_id=request_id, headers=headers)
    assert response.status_code == 202, response.text
    return polled(client, response.json()["id"], headers)


def polled(client, generation_id, headers=PLAYER):
    """생성 상태(완료면 assistant_message에 턴 API와 같은 메시지 ID·info)."""
    response = client.get(f"{GENERATIONS}/{generation_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def turns(client, session_id, headers=PLAYER):
    response = client.get(f"{SESSIONS}/{session_id}/turns", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def bubbles(message):
    return [(part["speaker_name"], part["body"]) for part in message["parts"]]


def generation_text(db, generation_id):
    return db.execute(
        "SELECT text FROM conversation_generations WHERE id = %s", (generation_id,)
    ).fetchone()["text"]


def frozen(db, session_id):
    """info 수정 전후로 바뀌면 안 되는 것: 메시지·말풍선 행, 생성 행의 원문 밖 칸
    (상태·청구·시각 등), 세션 시작 값 행, 전체 청구 대상 수."""

    def rows(table, columns="t.*"):
        row = f"row_to_json((SELECT r FROM (SELECT {columns}) r))::text"
        return f"""(SELECT md5(coalesce(string_agg({row}, '|' ORDER BY {row}), ''))
                    FROM {table} t WHERE t.session_id = %(s)s)"""

    generation_columns = (
        "t.id, t.status, t.billable, t.error_code, t.created_at, t.updated_at,"
        " t.reported_model, t.model_id, t.request_id"
    )
    return db.execute(
        f"""SELECT
             (SELECT count(*) FROM conversation_generations WHERE billable) AS billable,
             {rows("conversation_messages")} AS messages,
             {rows("conversation_message_parts")} AS parts,
             {rows("conversation_generations", generation_columns)} AS generations,
             {rows("conversation_scene_states")} AS scene,
             {rows("conversation_character_states")} AS characters""",
        {"s": session_id},
    ).fetchone()


def claimed(db, generation_id):
    return db.execute(
        """SELECT g.session_id, g.user_message_id, cs.owner_id FROM conversation_generations g
           JOIN conversation_sessions cs ON cs.id = g.session_id WHERE g.id = %s""",
        (generation_id,),
    ).fetchone()


def set_date_mode(db, session_id, mode, values):
    columns = {key: values.get(key) for key in DATE_COLUMNS}
    db.execute(
        """UPDATE conversation_scene_states
           SET date_mode = %s, era = %s, year = %s, month = %s, day = %s,
               reference_event = %s, elapsed_day = %s
           WHERE session_id = %s""",
        (mode, *columns.values(), session_id),
    )


def date_view(mode, **values):
    return {"mode": mode, **dict.fromkeys(DATE_COLUMNS), **values}


# ---- 순수 규칙: replace_info ----


@pytest.mark.parametrize(
    "text",
    [
        f"{BODY}\n\n```info\n{PARK_INFO}\n```",
        f"{BODY}\n```info\n{PARK_INFO}\n```\n",
        # 닫는 줄 뒤 글(본문으로 돌아가는 줄)은 그 자리에 그대로 남는다.
        f"{BODY}\n```info\n{PARK_INFO}\n```\n@도경: 한마디 더.",
        # 닫는 줄 없이 끝난 블록.
        f"{BODY}\n```info\n장소: 공원",
        # 앞쪽 info 블록까지 모두 빠지고 새 블록 하나만 맨 끝에 남는다(규칙 4~6 개정).
        f"{BODY}\n```info\n장소: 앞\n```\n@나레이터: 계속\n```info\n장소: 뒤\n```",
        # CRLF·펜스 앞뒤 공백.
        f"{BODY}\r\n\r\n  ```info \r\n장소: 공원\r\n```\t\r\n",
        # info 없는 응답: 맨 끝에 붙인다.
        BODY,
        f"{BODY}\n",
        f"{BODY}\n\n",
    ],
)
def test_chat_us_08_info_edit_replace_info_changes_only_the_block(text):
    lines = ["장소: 기차역", "[도경] 착장: - | 속마음: -"]
    replaced = replace_info(text, lines)
    body, info = split_info(replaced)
    assert info == "\n".join(lines)
    assert body == split_info(text)[0]
    assert parse_assistant_text(body, ["도경"]) == parse_assistant_text(
        split_info(text)[0], ["도경"]
    )
    # 다시 바꿔도 블록이 늘지 않는다.
    again = replace_info(replaced, ["장소: 집"])
    assert split_info(again) == (body, "장소: 집")
    assert again.count("```info") == 1
    assert again.endswith("```info\n장소: 집\n```")


def test_chat_us_08_info_edit_replace_info_writes_one_block_at_the_end():
    # 응답 중간 블록·끝 블록을 모두 빼고, 본문(블록 밖 줄) 뒤에 새 블록 하나를 쓴다.
    before = f"{BODY}\r\n```info\r\n장소: 앞\r\n```\r\n@도경: 끝.\r\n\r\n```info\r\n장소: 뒤\r\n```"
    replaced = replace_info(before, ["장소: 기차역"])
    assert replaced == f"{BODY}\n@도경: 끝.\n\n```info\n장소: 기차역\n```"


def test_chat_us_08_info_edit_replace_info_without_block_and_crlf_keeps_bubbles():
    # 블록이 없던 응답의 본문은 split_info 규칙 6대로 줄바꿈이 \n으로 맞춰진다. 말풍선은 같다.
    text = "@도경: 첫 줄\r\n둘째 줄\r\n@나레이터: 끝\r\n"
    replaced = replace_info(text, ["장소: 기차역"])
    assert replaced.startswith(text)
    body, info = split_info(replaced)
    assert info == "장소: 기차역"
    assert parse_assistant_text(body, ["도경"]) == parse_assistant_text(text, ["도경"])


# ---- API: 성공 ----


@pytest.mark.parametrize(
    "mode, start, turn_date, edit_date, date_line, scene_date",
    [
        (
            "calendar",
            {"year": 2026, "month": 11, "day": 22},
            "날짜: 2026년 11월 23일",
            {"year": 2026, "month": 2, "day": 28},
            "날짜: 2026년 2월 28일",
            date_view("calendar", year=2026, month=2, day=28),
        ),
        (
            "world",
            {"era": "대한", "year": 1898, "month": 3, "day": 1},
            "날짜: 대한 1898년 3월 2일",
            {"mode": "world", "era": "광무", "year": 1899, "month": 12, "day": 31},
            "날짜: 광무 1899년 12월 31일",
            date_view("world", era="광무", year=1899, month=12, day=31),
        ),
        (
            "elapsed",
            {"reference_event": "감금", "elapsed_day": 3},
            "날짜: 4일째",
            # 기준 사건을 빼면 지금 기준 사건(감금)을 둔다.
            {"elapsed_day": 5},
            "날짜: 감금 5일째",
            date_view("elapsed", reference_event="감금", elapsed_day=5),
        ),
        (
            "elapsed",
            {"reference_event": "감금", "elapsed_day": 3},
            "날짜: 4일째",
            {"mode": "elapsed", "reference_event": "탈출", "elapsed_day": 1},
            "날짜: 탈출 1일째",
            date_view("elapsed", reference_event="탈출", elapsed_day=1),
        ),
    ],
)
def test_chat_us_08_info_edit_happy_path_per_date_mode(
    client, db, session, recorder, mode, start, turn_date, edit_date, date_line, scene_date
):
    set_date_mode(db, session, mode, start)
    generation = turn(
        client,
        session,
        recorder,
        f"{turn_date}\n시간: 12시\n장소: 공원 벤치\n[Bea] 착장: 노란 우비 | 속마음: 배고프다",
        "공원으로.",
        "turn-0001",
    )
    message_id = generation["assistant_message"]["id"]
    before_room = turns(client, session)
    before = frozen(db, session)
    calls = len(recorder.requests)

    response = client.put(
        info_url(session, message_id),
        headers=PLAYER,
        json={
            "date": edit_date,
            "time": {"hour": 7, "minute": 5},
            "place": "기차역",
            "characters": [{"name": "Bea", "inner_thought": "설렌다"}],
        },
    )

    assert response.status_code == 200, response.text
    lines = [
        date_line,
        "시간: 07:05",
        "장소: 기차역",
        "[도경] 착장: 젖은 코트 | 속마음: 피곤하다",
        "[Bea] 착장: 노란 우비 | 속마음: 설렌다",
    ]
    expected_scene = {
        "date": scene_date,
        "time": {"hour": 7, "minute": 5},
        "place": "기차역",
        "characters": [
            {"name": "도경", "clothing": "젖은 코트", "inner_thought": "피곤하다"},
            {"name": "Bea", "clothing": "노란 우비", "inner_thought": "설렌다"},
        ],
    }
    result = response.json()
    assert result["message_id"] == message_id
    assert result["info"]["text"] == "\n".join(lines)
    assert result["info"] == {"text": "\n".join(lines), **expected_scene}
    assert result["current_scene"] == expected_scene
    # 생성 원문의 info만 바뀌고 본문(블록 앞 빈 줄 포함)은 글자 단위로 그대로다.
    stored = generation_text(db, generation["id"])
    assert split_info(stored) == (f"{BODY}\n", "\n".join(lines))
    assert stored.startswith(f"{BODY}\n\n```info\n")
    # 턴 API도 같은 값을 돌려주고, 말풍선·다른 메시지는 그대로다.
    room = turns(client, session)
    assert room["current_scene"] == expected_scene
    assert room["messages"][1]["info"] == result["info"]
    assert [bubbles(m) for m in room["messages"]] == [bubbles(m) for m in before_room["messages"]]
    assert room["messages"][0] == before_room["messages"][0]
    assert room["messages"][1]["created_at"] == before_room["messages"][1]["created_at"]
    # 청구·상태·시각·말풍선 행·시작 값은 그대로이고 모델을 부르지 않았다.
    assert frozen(db, session) == before
    assert len(recorder.requests) == calls
    # 다음 요청의 [현재 장면]은 고친 값이다(날짜 방식별 날짜 줄 포함).
    turn(client, session, recorder, None, "출발.", "turn-0002")
    assert scene_block(recorder.requests[-1]) == "\n".join(["[현재 장면]", *lines])


def test_chat_us_08_info_edit_is_what_current_scene_and_next_prompt_are_built_from(
    client, db, session, recorder, monkeypatch
):
    contexts = []
    load = service.load_prompt_context

    def spy(conn, generation):
        contexts.append(context := load(conn, generation))
        return context

    monkeypatch.setattr(service, "load_prompt_context", spy)
    first = turn(client, session, recorder, PARK_INFO, "공원으로.", "turn-0001")
    response = client.put(
        info_url(session, first["assistant_message"]["id"]),
        headers=PLAYER,
        json={"place": "공원 매점", "characters": [{"name": "도경", "clothing": "검은 코트"}]},
    )
    assert response.status_code == 200, response.text
    edited = response.json()["current_scene"]
    assert edited == {
        **PARK_VIEW,
        "place": "공원 매점",
        "characters": [
            {"name": "도경", "clothing": "검은 코트", "inner_thought": "피곤하다"},
            {"name": "Bea", "clothing": "노란 우비", "inner_thought": "배고프다"},
        ],
    }
    assert turns(client, session)["current_scene"] == edited
    second = turn(client, session, recorder, None, "뭐 먹을까?", "turn-0002")
    # 같은 _current_scene: 턴 API current_scene = 다음 요청 문맥의 장면 = [현재 장면] 블록.
    assert scene_view(contexts[-1].scene, contexts[-1].character_states) == edited
    expected_block = [
        "[현재 장면]",
        "날짜: 2026년 11월 23일",
        "시간: 12시",
        "장소: 공원 매점",
        "[도경] 착장: 검은 코트 | 속마음: 피곤하다",
        "[Bea] 착장: 노란 우비 | 속마음: 배고프다",
    ]
    assert scene_block(recorder.requests[-1]) == "\n".join(expected_block)
    # 그 요청을 저장된 값으로 다시 만들어도 같다(프롬프트 빌더 경로).
    rebuilt = service.build_messages(service.load_prompt_context(db, claimed(db, second["id"])))
    assert rebuilt == recorder.requests[-1].messages
    # info 없는 다음 응답 뒤에도 고친 값이 이어진다.
    assert turns(client, session)["current_scene"] == edited


def test_chat_us_08_info_edit_omitted_fields_keep_reply_values(client, db, session, recorder):
    generation = turn(client, session, recorder, PARK_INFO, "공원으로.", "turn-0001")
    message_id = generation["assistant_message"]["id"]
    before = turns(client, session)["current_scene"]
    # 빈 수정: 값은 그대로이고, 블록은 그 응답까지의 누적 장면 전체로 다시 쓴다(이용자 줄은 빠진다).
    response = client.put(info_url(session, message_id), headers=PLAYER, json={})
    assert response.status_code == 200, response.text
    assert response.json()["current_scene"] == before
    assert split_info(generation_text(db, generation["id"]))[1] == "\n".join(PARK_LINES)
    # null도 '그대로'다(값을 지우지 않는다).
    response = client.put(
        info_url(session, message_id),
        headers=PLAYER,
        json={
            "date": None,
            "time": None,
            "place": None,
            "characters": [{"name": "Bea", "clothing": None, "inner_thought": None}],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["current_scene"] == before
    # 시간만 고치면 나머지 줄은 그대로다. 분을 빼면 분을 모르는 시간이다.
    response = client.put(info_url(session, message_id), headers=PLAYER, json={"time": {"hour": 9}})
    assert response.status_code == 200, response.text
    assert response.json()["current_scene"] == {**before, "time": {"hour": 9, "minute": None}}
    info = split_info(generation_text(db, generation["id"]))[1]
    assert info == "\n".join([PARK_LINES[0], "시간: 9시", *PARK_LINES[2:]])
    # 같은 응답을 여러 번 고쳐도 info 블록은 하나다.
    assert generation_text(db, generation["id"]).count("```info") == 1


@pytest.mark.parametrize(
    "body",
    [
        BODY,
        f"{BODY}\n",
        "@도경: 첫 줄\r\n둘째 줄\r\n@나레이터: 끝",
    ],
    ids=["plain", "trailing-newline", "crlf"],
)
def test_chat_us_08_info_edit_reply_without_info_gets_one_appended(
    client, db, session, recorder, body
):
    generation = turn(client, session, recorder, None, "안녕.", "turn-0001", body=body)
    message_id = generation["assistant_message"]["id"]
    before_room = turns(client, session)
    assert before_room["messages"][1]["info"] is None
    before = frozen(db, session)
    response = client.put(info_url(session, message_id), headers=PLAYER, json={"place": "기차역"})
    assert response.status_code == 200, response.text
    stored = generation_text(db, generation["id"])
    # 원래 원문 뒤에 블록이 붙는다(원문 앞부분은 한 글자도 바뀌지 않는다).
    assert stored.startswith(body)
    new_body, info = split_info(stored)
    assert info == "\n".join(
        [
            "날짜: 2026년 11월 22일",
            "시간: 21:05",
            "장소: 기차역",
            "[도경] 착장: 젖은 코트 | 속마음: 피곤하다",
            "[Bea] 착장: - | 속마음: -",
        ]
    )
    names = ["도경", "Bea"]
    assert parse_assistant_text(new_body, names) == parse_assistant_text(body, names)
    room = turns(client, session)
    assert room["messages"][1]["info"]["place"] == "기차역"
    assert room["current_scene"]["place"] == "기차역"
    assert [bubbles(m) for m in room["messages"]] == [bubbles(m) for m in before_room["messages"]]
    assert frozen(db, session) == before


@pytest.mark.parametrize(
    "text",
    [
        f"{BODY}\n```info\n{PARK_INFO}\n```\n@나레이터: *덧붙인다.*",
        f"{BODY}\n```info\n장소: 공원 벤치",
        # 실제 모델: 응답 중간 info 블록 → 발화자 블록 → 맨 끝 info 블록(규칙 4~6 개정).
        f"{BODY}\n```info\n장소: 앞\n```\n...\n@나레이터: *더.*\n\n```info\n{PARK_INFO}\n```",
    ],
    ids=["text-after-block", "unclosed-block", "mid-reply-block"],
)
def test_chat_us_08_info_edit_bubbles_stay_byte_identical(client, db, session, recorder, text):
    recorder.chunks = [text]
    generation = polled(client, send(client, session, [user_part("공원으로.")]).json()["id"])
    message_id = generation["assistant_message"]["id"]
    old_body = split_info(text)[0]
    before_parts = turns(client, session)["messages"][1]["parts"]
    response = client.put(info_url(session, message_id), headers=PLAYER, json={"place": "기차역"})
    assert response.status_code == 200, response.text
    stored = generation_text(db, generation["id"])
    new_body, info = split_info(stored)
    assert new_body == old_body
    assert "장소: 기차역" in info.splitlines()
    # 블록은 하나만, 맨 끝에 남는다.
    assert stored.count("```info") == 1
    assert stored.endswith("\n```")
    assert "```" not in str(before_parts) and "장소" not in str(before_parts)
    assert turns(client, session)["messages"][1]["parts"] == before_parts


def test_chat_us_08_info_edit_latest_completed_reply_after_failed_turn(
    client, db, session, recorder
):
    # 뒤 요청이 빈 응답으로 실패해도 마지막 완료 응답은 고칠 수 있고, 재시도 요청이 고친 값을 쓴다.
    first = turn(client, session, recorder, PARK_INFO, "공원으로.", "turn-0001")
    recorder.chunks = ["\n"]
    sent = send(client, session, [user_part("배고파.")], request_id="turn-0002").json()
    failed = polled(client, sent["id"])
    assert failed["status"] == "failed"
    response = client.put(
        info_url(session, first["assistant_message"]["id"]), headers=PLAYER, json={"place": "매점"}
    )
    assert response.status_code == 200, response.text
    recorder.chunks = info_reply(BODY, None)
    retried = client.post(
        f"{GENERATIONS}/{failed['id']}/retry",
        headers=PLAYER,
        json={"request_id": "retry-0001"},
    )
    assert retried.status_code == 202, retried.text
    assert "장소: 매점" in scene_block(recorder.requests[-1]).splitlines()


# ---- API: 거절 ----


def problem(response, status, code):
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["code"] == code, body
    return body


def test_chat_us_08_info_edit_older_reply_is_409(client, db, session, recorder):
    first = turn(client, session, recorder, PARK_INFO, "공원으로.", "turn-0001")
    second = turn(client, session, recorder, None, "계속.", "turn-0002")
    before = generation_text(db, first["id"])
    response = client.put(
        info_url(session, first["assistant_message"]["id"]), headers=PLAYER, json={"place": "집"}
    )
    body = problem(response, 409, "SCENE_INFO_NOT_LATEST_REPLY")
    assert body["latest_message_id"] == second["assistant_message"]["id"]
    assert generation_text(db, first["id"]) == before
    # 마지막 응답은 고칠 수 있다.
    response = client.put(
        info_url(session, second["assistant_message"]["id"]), headers=PLAYER, json={"place": "집"}
    )
    assert response.status_code == 200, response.text


def test_chat_us_08_info_edit_during_generation_is_409_and_stale_one_is_expired_first(
    client, db, session, recorder, monkeypatch
):
    first = turn(client, session, recorder, PARK_INFO, "공원으로.", "turn-0001")
    message_id = first["assistant_message"]["id"]
    monkeypatch.setattr(worker, "submit", lambda job, *args: None)
    pending = send(client, session, [user_part("계속.")], request_id="turn-0002").json()
    before = frozen(db, session)
    for status in ("pending", "streaming"):
        db.execute(
            "UPDATE conversation_generations SET status = %s WHERE id = %s",
            (status, pending["id"]),
        )
        response = client.put(info_url(session, message_id), headers=PLAYER, json={"place": "집"})
        body = problem(response, 409, "GENERATION_IN_PROGRESS")
        assert body["active_generation_id"] == pending["id"]
    db.execute(
        "UPDATE conversation_generations SET status = 'pending' WHERE id = %s", (pending["id"],)
    )
    assert frozen(db, session) == before
    assert split_info(generation_text(db, first["id"]))[1] == PARK_INFO
    # 멈춘 생성은 전송과 같은 규칙으로 먼저 정리한 뒤 고칠 수 있다. 늦게 깨어난 작업은 아무것도
    # 바꾸지 못한다(pending이 아니므로).
    db.execute(
        "UPDATE conversation_generations SET updated_at = now() - interval '1 hour' WHERE id = %s",
        (pending["id"],),
    )
    response = client.put(info_url(session, message_id), headers=PLAYER, json={"place": "집"})
    assert response.status_code == 200, response.text
    service.run_generation(pending["id"])
    assert (
        db.execute(
            "SELECT status FROM conversation_generations WHERE id = %s", (pending["id"],)
        ).fetchone()["status"]
        == "failed"
    )
    assert turns(client, session)["current_scene"]["place"] == "집"


@pytest.mark.parametrize("intruder", [AUTHOR, STRANGER], ids=["story_author", "stranger"])
def test_chat_us_08_info_edit_other_account_is_404_without_side_effects(
    client, db, saved_story, session, recorder, intruder
):
    db.execute("INSERT INTO user_play_settings VALUES (3, '지나가는이')")
    story, intro, _ = saved_story
    own = start_session(client, story, intro, headers=intruder)
    generation = turn(client, session, recorder, PARK_INFO, "공원으로.", "turn-0001")
    message_id = generation["assistant_message"]["id"]
    before = frozen(db, session)
    text = generation_text(db, generation["id"])
    # 남의 세션 경로, 그리고 자기 세션 경로에 남의 메시지 ID를 넣은 경우 모두 404다.
    refused = [
        client.put(info_url(session, message_id), headers=intruder, json={"place": "남의 방"}),
        client.put(info_url(own, message_id), headers=intruder, json={"place": "남의 방"}),
    ]
    problem(refused[0], 404, "CONVERSATION_SESSION_NOT_FOUND")
    problem(refused[1], 404, "CONVERSATION_MESSAGE_NOT_FOUND")
    for response in refused:
        assert "공원" not in response.text and "도경" not in response.text
    assert frozen(db, session) == before
    assert generation_text(db, generation["id"]) == text


def test_chat_us_08_info_edit_message_must_be_completed_assistant(client, db, session, recorder):
    first = turn(client, session, recorder, PARK_INFO, "공원으로.", "turn-0001")
    room = turns(client, session)
    user_message_id = room["messages"][0]["id"]
    problem(
        client.put(info_url(session, user_message_id), headers=PLAYER, json={"place": "집"}),
        422,
        "SCENE_INFO_NOT_ASSISTANT_MESSAGE",
    )
    problem(
        client.put(info_url(session, 999999), headers=PLAYER, json={"place": "집"}),
        404,
        "CONVERSATION_MESSAGE_NOT_FOUND",
    )
    # 실패한 응답(빈 응답)의 메시지는 완료가 아니다.
    recorder.chunks = ["\n"]
    failed = send(client, session, [user_part("배고파.")], request_id="turn-0002").json()
    failed_message = db.execute(
        "SELECT assistant_message_id FROM conversation_generations WHERE id = %s", (failed["id"],)
    ).fetchone()["assistant_message_id"]
    problem(
        client.put(info_url(session, failed_message), headers=PLAYER, json={"place": "집"}),
        409,
        "SCENE_INFO_REPLY_NOT_COMPLETED",
    )
    assert split_info(generation_text(db, first["id"]))[1] == PARK_INFO


def test_chat_us_08_info_edit_read_only_conversation_is_409(
    client, db, saved_story, session, recorder
):
    generation = turn(client, session, recorder, PARK_INFO, "공원으로.", "turn-0001")
    db.execute("UPDATE stories SET deleted_at = now() WHERE id = %s", (saved_story[0],))
    response = client.put(
        info_url(session, generation["assistant_message"]["id"]),
        headers=PLAYER,
        json={"place": "집"},
    )
    problem(response, 409, "CONVERSATION_READ_ONLY")
    assert split_info(generation_text(db, generation["id"]))[1] == PARK_INFO


LONG = "가" * 31
INVALID = "SCENE_INFO_INVALID_VALUE"
MISMATCH = "SCENE_INFO_DATE_MODE_MISMATCH"
UNKNOWN = "SCENE_INFO_UNKNOWN_CHARACTER"
WORLD = ("world", {"era": "대한", "year": 1898, "month": 3, "day": 1})
ELAPSED = ("elapsed", {"reference_event": "감금", "elapsed_day": 3})
NO_DATE = (None, {})


@pytest.mark.parametrize(
    "mode, body, code, field",
    [
        # 날짜 방식은 바꿀 수 없다.
        (
            None,
            {"date": {"mode": "world", "era": "대한", "year": 1, "month": 1, "day": 1}},
            MISMATCH,
            "date.mode",
        ),
        (
            WORLD,
            {"date": {"mode": "calendar", "year": 2026, "month": 1, "day": 1}},
            MISMATCH,
            "date.mode",
        ),
        (NO_DATE, {"date": {"year": 2026, "month": 1, "day": 1}}, MISMATCH, "date.mode"),
        # 달력: 2월 28일(윤년 없음)·달별 날 수·범위·빠진 칸·다른 방식 칸.
        (None, {"date": {"year": 2028, "month": 2, "day": 29}}, INVALID, "date"),
        (None, {"date": {"year": 2026, "month": 4, "day": 31}}, INVALID, "date"),
        (None, {"date": {"year": 2026, "month": 13, "day": 1}}, INVALID, "date"),
        (None, {"date": {"year": 0, "month": 1, "day": 1}}, INVALID, "date"),
        (None, {"date": {"year": 10000, "month": 1, "day": 1}}, INVALID, "date"),
        (None, {"date": {"year": 2026, "month": 11}}, INVALID, "date"),
        (None, {"date": {"era": "대한", "year": 2026, "month": 1, "day": 1}}, INVALID, "date.era"),
        (
            None,
            {"date": {"year": 2026, "month": 1, "day": 1, "elapsed_day": 3}},
            INVALID,
            "date.elapsed_day",
        ),
        # 세계관: 연호 필수·자리 표시 값·10자 상한·2월 29일.
        (WORLD, {"date": {"year": 1898, "month": 3, "day": 2}}, INVALID, "date"),
        (WORLD, {"date": {"era": "<연호>", "year": 1898, "month": 3, "day": 2}}, INVALID, "date"),
        (WORLD, {"date": {"era": "가" * 11, "year": 1898, "month": 3, "day": 2}}, INVALID, "date"),
        (WORLD, {"date": {"era": "대한", "year": 1898, "month": 2, "day": 29}}, INVALID, "date"),
        # 경과일: 일차 범위·기준 사건 상한·자리 표시 값·연월일 칸.
        (ELAPSED, {"date": {"elapsed_day": 0}}, INVALID, "date"),
        (ELAPSED, {"date": {"elapsed_day": 1000000}}, INVALID, "date"),
        (ELAPSED, {"date": {"reference_event": "가" * 11, "elapsed_day": 2}}, INVALID, "date"),
        (ELAPSED, {"date": {"reference_event": "미정", "elapsed_day": 2}}, INVALID, "date"),
        (ELAPSED, {"date": {"reference_event": ""}}, INVALID, "date"),
        (ELAPSED, {"date": {"elapsed_day": 2, "year": 2026}}, INVALID, "date.year"),
        # 시간.
        (None, {"time": {"hour": 24, "minute": 0}}, INVALID, "time"),
        (None, {"time": {"hour": -1}}, INVALID, "time"),
        (None, {"time": {"hour": 12, "minute": 60}}, INVALID, "time"),
        # 장소: 빈 값·자리 표시 값·상한·줄바꿈·앞뒤 공백.
        (None, {"place": ""}, INVALID, "place"),
        (None, {"place": "-"}, INVALID, "place"),
        (None, {"place": "미정"}, INVALID, "place"),
        (None, {"place": "알 수 없음."}, INVALID, "place"),
        (None, {"place": "<장소>"}, INVALID, "place"),
        (None, {"place": LONG}, INVALID, "place"),
        (None, {"place": "공원\n벤치"}, INVALID, "place"),
        (None, {"place": "공원\u2028벤치"}, INVALID, "place"),
        (None, {"place": " 공원"}, INVALID, "place"),
        # 캐릭터: 모르는 이름(이용자 캐릭터 포함)·중복·상한·자리 표시 값·`| 속마음:`.
        (
            None,
            {"characters": [{"name": "민서", "clothing": "교복"}]},
            UNKNOWN,
            "characters[0].name",
        ),
        (
            None,
            {"characters": [{"name": "하린", "clothing": "교복"}]},
            UNKNOWN,
            "characters[0].name",
        ),
        (
            None,
            {"characters": [{"name": "도경"}, {"name": "도경", "clothing": "셔츠"}]},
            INVALID,
            "characters[1].name",
        ),
        (
            None,
            {"characters": [{"name": "도경", "clothing": "가" * 301}]},
            INVALID,
            "characters[0].clothing",
        ),
        (
            None,
            {"characters": [{"name": "Bea", "inner_thought": "가" * 101}]},
            INVALID,
            "characters[0].inner_thought",
        ),
        (
            None,
            {"characters": [{"name": "도경", "clothing": "모름"}]},
            INVALID,
            "characters[0].clothing",
        ),
        (
            None,
            {"characters": [{"name": "도경", "inner_thought": "<속마음>"}]},
            INVALID,
            "characters[0].inner_thought",
        ),
        (
            None,
            {"characters": [{"name": "도경", "clothing": "셔츠 | 속마음: 몰래"}]},
            INVALID,
            "characters[0].clothing",
        ),
        (
            None,
            {"characters": [{"name": "도경", "clothing": "셔츠\n바지"}]},
            INVALID,
            "characters[0].clothing",
        ),
    ],
)
def test_chat_us_08_info_edit_invalid_values_are_422(
    client, db, session, recorder, mode, body, code, field
):
    if mode == NO_DATE:
        set_date_mode(db, session, None, {})
    elif mode is not None:
        set_date_mode(db, session, *mode)
    generation = turn(client, session, recorder, PARK_INFO, "공원으로.", "turn-0001")
    text = generation_text(db, generation["id"])
    scene = turns(client, session)["current_scene"]
    response = client.put(
        info_url(session, generation["assistant_message"]["id"]), headers=PLAYER, json=body
    )
    assert problem(response, 422, code)["field"] == field
    assert generation_text(db, generation["id"]) == text
    assert turns(client, session)["current_scene"] == scene


@pytest.mark.parametrize(
    "body",
    [
        {"time": {"hour": "12"}},
        {"time": {"hour": 12.0}},
        {"time": {"minute": 5}},
        {"place": 3},
        {"place": "공원\0벤치"},
        {"characters": [{"name": "도경", "clothing": "코트\0"}]},
        {"characters": [{"clothing": "코트"}]},
        {"text": "장소: 공원"},
        {"date": {"year": 2026, "month": 1, "day": 1, "weekday": "월"}},
        {"characters": [{"name": "도경"}] * 101},
    ],
)
def test_chat_us_08_info_edit_malformed_body_is_validation_error(
    client, db, session, recorder, body
):
    generation = turn(client, session, recorder, PARK_INFO, "공원으로.", "turn-0001")
    text = generation_text(db, generation["id"])
    response = client.put(
        info_url(session, generation["assistant_message"]["id"]), headers=PLAYER, json=body
    )
    problem(response, 422, "VALIDATION_ERROR")
    assert generation_text(db, generation["id"]) == text
