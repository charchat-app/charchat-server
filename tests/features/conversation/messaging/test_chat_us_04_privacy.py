"""CHAT-US-04 내 대화의 비공개 보호.

다른 계정의 요청은 대화 경로마다 거절하고 아무것도 남기지 않는다.
세션 주인은 PLAYER(2), 스토리 제작자는 AUTHOR(1), 관계없는 계정은 STRANGER(3)다.
"""

from time import monotonic

import pytest
from messaging_helpers import AUTHOR, GENERATIONS, PLAYER, SESSIONS, send, start_session, user_part

STRANGER = {"X-User-Id": "3"}


def snapshot(db, session_id):
    """거절 요청 전후로 바뀌면 안 되는 값: 메시지·생성 기록·청구 대상·세션 행(선택 모델 포함)과
    세션의 장면·캐릭터 상태·관계·호감도."""

    def rows(table):
        row = "row_to_json(t)::text"
        return f"""(SELECT md5(coalesce(string_agg({row}, '|' ORDER BY {row}), ''))
                    FROM {table} t WHERE t.session_id = %(s)s)"""

    return db.execute(
        f"""SELECT
             (SELECT count(*) FROM conversation_messages) AS messages,
             (SELECT count(*) FROM conversation_generations) AS generations,
             (SELECT count(*) FROM conversation_generations WHERE billable) AS billable,
             (SELECT row_to_json(s)::text FROM conversation_sessions s
               WHERE s.id = %(s)s) AS session,
             {rows("conversation_messages")} AS session_messages,
             {rows("conversation_message_parts")} AS parts,
             {rows("conversation_user_profiles")} AS profile,
             {rows("conversation_scene_states")} AS scene,
             {rows("conversation_character_states")} AS characters,
             {rows("conversation_relationships")} AS relationships,
             {rows("conversation_affinities")} AS affinities""",
        {"s": session_id},
    ).fetchone()


def other_model_id(db, session_id):
    return db.execute(
        """SELECT id FROM models
           WHERE id <> (SELECT selected_model_id FROM conversation_sessions WHERE id = %s)
           ORDER BY id LIMIT 1""",
        (session_id,),
    ).fetchone()["id"]


@pytest.mark.parametrize("intruder", [AUTHOR, STRANGER], ids=["story_author", "stranger"])
def test_chat_us_04_ac01_ac03_other_account_is_refused_on_every_route_without_side_effects(
    client, db, saved_story, session, recorder, intruder
):
    # 침입자도 같은 스토리에 자기 대화방이 있다(목록이 비어서 우연히 통과하지 않게).
    db.execute("INSERT INTO user_play_settings VALUES (3, '지나가는이')")
    story, intro, _ = saved_story
    own_session = start_session(client, story, intro, headers=intruder)
    # 세션 주인이 한 번 대화해 둔다(완료된 응답 1건, 모델 호출 1번).
    generation = send(client, session, [user_part("내 이야기")], request_id="owner-0001").json()
    assert client.get(f"{GENERATIONS}/{generation['id']}", headers=PLAYER).json()["status"] == (
        "completed"
    )
    model_calls = len(recorder.requests)
    before = snapshot(db, session)
    new_model = other_model_id(db, session)

    refused = {
        "세션 조회": client.get(f"{SESSIONS}/{session}", headers=intruder),
        "대화 기록": client.get(f"{SESSIONS}/{session}/turns", headers=intruder),
        "메시지 전송": send(
            client, session, [user_part("끼어들기")], request_id="intruder-0001", headers=intruder
        ),
        "요청 상태": client.get(f"{GENERATIONS}/{generation['id']}", headers=intruder),
        "요청 상태 기다리기": client.get(
            f"{GENERATIONS}/{generation['id']}", params={"wait": 5}, headers=intruder
        ),
        "요청 ID 조회": client.get(
            f"{SESSIONS}/{session}/generations",
            params={"request_id": "owner-0001"},
            headers=intruder,
        ),
        "재시도": client.post(
            f"{GENERATIONS}/{generation['id']}/retry",
            json={"request_id": "intruder-retry-01"},
            headers=intruder,
        ),
        "모델 변경": client.put(
            f"{SESSIONS}/{session}/model", json={"model_id": new_model}, headers=intruder
        ),
    }
    # 롱 폴링도 기다리기 전에 주인을 확인한다(5초를 기다리지 않고 바로 거절).
    started = monotonic()
    timed = client.get(f"{GENERATIONS}/{generation['id']}", params={"wait": 5}, headers=intruder)
    assert timed.status_code == 404
    assert monotonic() - started < 2
    statuses = {name: response.status_code for name, response in refused.items()}
    assert statuses == dict.fromkeys(refused, 404)
    # 거절 응답에 주인의 대화 내용이 섞여 나오지 않는다.
    for response in refused.values():
        assert "내 이야기" not in response.text

    # AC01: 목록에도 남의 대화가 보이지 않는다(제작자도 예외 없음).
    listed = client.get(SESSIONS, headers=intruder)
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["sessions"]] == [own_session]

    # AC03: 메시지·생성 기록·청구·모델·세션 상태가 그대로이고 모델을 부르지 않았다.
    assert snapshot(db, session) == before
    assert len(recorder.requests) == model_calls

    # 주인은 그대로 이어서 쓸 수 있다.
    turns = client.get(f"{SESSIONS}/{session}/turns", headers=PLAYER)
    assert turns.status_code == 200
    assert [m["role"] for m in turns.json()["messages"]] == ["user", "assistant"]
