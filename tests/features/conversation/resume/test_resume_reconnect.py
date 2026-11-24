"""CHAT-US-03 재접속 이어하기: 저장 기록·현재 상태 복원, 기존 request_id 확인, 생성 중 이어 받기."""

import threading
from time import monotonic, sleep

from fastapi.testclient import TestClient
from resume_helpers import (
    AUTHOR,
    GENERATIONS,
    PLAYER,
    SESSIONS,
    GatedProvider,
    count,
    lookup,
    send,
    start,
)

from app.features.conversation.messaging import providers, worker
from app.features.conversation.messaging import service as messaging_service
from app.main import create_app

STATE_QUERIES = {
    "scene": "SELECT place, year, month, day, hour, minute FROM conversation_scene_states",
    "characters": """SELECT name_snapshot, clothing, inner_thought
                     FROM conversation_character_states""",
    "affinities": "SELECT target_kind, affinity FROM conversation_affinities",
    "relationships": "SELECT source_kind, target_kind, label FROM conversation_relationships",
    "profile": "SELECT source_kind, name, description, image_id FROM conversation_user_profiles",
    "session": """SELECT id, selected_model_id, source_intro_id, intro_messages_snapshot
                  FROM conversation_sessions""",
}


def _state(db):
    return {
        key: db.execute(query + " ORDER BY 1").fetchall() for key, query in STATE_QUERIES.items()
    }


def _wait_for(predicate, timeout=10.0):
    deadline = monotonic() + timeout
    while not predicate():
        assert monotonic() < deadline, "기다린 상태가 되지 않았습니다."
        sleep(0.02)


def _generation(db, generation_id):
    return db.execute(
        "SELECT status, text, billable FROM conversation_generations WHERE id = %s",
        (generation_id,),
    ).fetchone()


def test_chat_us_03_ac03_ac04_reconnect_restores_saved_reply_and_current_state(
    client, db, db_url, story
):
    session = start(client, story)
    sent = send(client, session, "어디 갔었어?")
    assert sent.status_code == 202
    assert _generation(db, sent.json()["id"])["status"] == "completed"
    before_turns = client.get(f"{SESSIONS}/{session}/turns", headers=PLAYER).json()
    before_state = _state(db)
    # 시작 뒤 제작 원본·기본 이름이 바뀌어도 재접속은 세션 저장값을 쓴다(AC03).
    story_id, intro, preset = story
    db.execute("UPDATE intro_scene_settings SET place = '바뀐 장소' WHERE intro_id = %s", (intro,))
    db.execute("UPDATE character_initial_affinities SET affinity_input = '99'")
    db.execute("UPDATE character_initial_relationships SET label = '바뀐 관계'")
    db.execute("UPDATE story_user_presets SET description = '바뀐 설명' WHERE id = %s", (preset,))
    db.execute("UPDATE intro_messages SET body = '바뀐 인트로'")
    db.execute("INSERT INTO user_play_settings VALUES (2, '바뀐 이름')")
    db.execute("UPDATE models SET provider_model_id = provider_model_id WHERE name = '테라'")
    counts = {t: count(db, t) for t in ("conversation_sessions", "conversation_generations")}

    # 화면을 닫고 같은 계정으로 다시 연결한다.
    with TestClient(create_app(db_url)) as again:
        listed = again.get(SESSIONS, headers=PLAYER).json()["sessions"]
        assert [row["id"] for row in listed] == [session]
        room = again.get(f"{SESSIONS}/{session}", headers=PLAYER).json()
        turns = again.get(f"{SESSIONS}/{session}/turns", headers=PLAYER).json()
        # 다시 열기를 반복해도 인트로는 세션 스냅샷 한 번뿐이다(AC04).
        reopened = again.get(f"{SESSIONS}/{session}", headers=PLAYER).json()

    assert room["model"] == turns["model"] == "루나"
    assert room["play_name"] == "민서"
    assert [m["body"] for m in room["messages"]] == ["비가 내린다.", "왔어, 민서?"]
    assert reopened["messages"] == room["messages"]
    assert turns["messages"] == before_turns["messages"]
    assert [m["role"] for m in turns["messages"]] == ["user", "assistant"]
    reply_text = "\n".join(p["body"] for p in turns["messages"][1]["parts"])
    assert "어디 갔었어?" in reply_text
    # 저장 이력에는 인트로가 메시지로 들어가지 않는다(AC04).
    assert all("비가 내린다." not in p["body"] for m in turns["messages"] for p in m["parts"])
    assert turns["active_generation"] is None
    assert turns["last_failed_generation"] is None
    assert _state(db) == before_state
    assert {t: count(db, t) for t in counts} == counts


def test_chat_us_03_ac03_ac09_reply_completed_while_away_is_restored_by_request_id(
    client, db, story, monkeypatch
):
    session = start(client, story)
    monkeypatch.setattr(worker, "submit", lambda job, *args: None)
    # 전송 응답을 받지 못한 채 앱을 닫았다고 본다. 생성은 서버에서 끝난다.
    assert send(client, session, "나가 있을게", request_id="lost-response-1").status_code == 202
    generation = db.execute("SELECT id FROM conversation_generations").fetchone()["id"]
    messaging_service.run_generation(generation)
    assert _generation(db, generation)["status"] == "completed"

    found = lookup(client, session, "lost-response-1")
    assert found.status_code == 200
    [result] = found.json()["generations"]
    assert result["id"] == generation
    assert result["status"] == "completed"
    assert result["text"] == _generation(db, generation)["text"]
    assert result["assistant_message"]["parts"][0]["speaker_name"] == "도경"
    turns = client.get(f"{SESSIONS}/{session}/turns", headers=PLAYER).json()
    assert turns["messages"][-1] == result["assistant_message"]
    # 확인은 조회만 한다. 새 생성·청구가 없다.
    assert count(db, "conversation_generations") == 1
    assert (
        db.execute("SELECT count(*) AS n FROM conversation_generations WHERE billable").fetchone()[
            "n"
        ]
        == 1
    )


def test_chat_us_03_ac09_unknown_request_id_returns_empty_without_creating(client, db, story):
    session = start(client, story)
    assert send(client, session, "접수됨", request_id="accepted-0001").status_code == 202
    before = count(db, "conversation_generations"), count(db, "conversation_messages")
    missing = lookup(client, session, "never-arrived-1")
    assert missing.status_code == 200
    assert missing.json() == {"generations": []}
    assert (count(db, "conversation_generations"), count(db, "conversation_messages")) == before


def test_chat_us_03_ac09_lookup_is_scoped_to_owner_and_session(client, db, story):
    mine = start(client, story)
    theirs = start(client, story, headers=AUTHOR, play_name="작가")
    assert send(client, theirs, "작가 메시지", request_id="shared-req-1", headers=AUTHOR)
    assert lookup(client, theirs, "shared-req-1").status_code == 404
    assert lookup(client, mine, "shared-req-1").json() == {"generations": []}
    assert lookup(client, 999999, "shared-req-1").status_code == 404
    bad = lookup(client, mine, "bad id!")
    assert bad.status_code == 422
    assert bad.json()["code"] == "VALIDATION_ERROR"
    assert client.get(f"{SESSIONS}/{mine}/generations", headers=PLAYER).status_code == 422
    assert lookup(client, mine, "shared-req-1", headers={}).status_code == 401


def test_chat_us_03_ac09_ac10_ac11_ac13_reconnect_during_generation_streams_rest_once(
    client, db, story, monkeypatch
):
    session = start(client, story)
    provider = GatedProvider("@도경: 이탈 중에 만든 앞부분 ", "그리고 이어진 뒷부분\n@나레이터: 끝")
    monkeypatch.setattr(providers, "get_provider", lambda: provider)
    monkeypatch.setattr(worker, "submit", lambda job, *args: None)
    sent = send(client, session, "생성 중에 나갈게", request_id="leave-midway-1")
    generation = sent.json()["id"]
    runner = threading.Thread(target=messaging_service.run_generation, args=(generation,))
    runner.start()
    try:
        # 화면을 떠난 동안 서버가 앞부분을 만든다.
        _wait_for(lambda: _generation(db, generation)["text"] == provider.first)

        # 목록에서 생성 중임을 알 수 있다(AC11).
        row = client.get(SESSIONS, headers=PLAYER).json()["sessions"][0]
        assert row["id"] == session
        assert row["generating"] is True
        assert provider.first.strip() not in str(row)

        # 재접속: 기존 request_id로 상태를 확인하고(AC09) 처리 중이면 이어 받는다(AC10).
        [found] = lookup(client, session, "leave-midway-1").json()["generations"]
        assert found["id"] == generation
        assert found["status"] == "streaming"
        assert found["assistant_message"] is None
        turns = client.get(f"{SESSIONS}/{session}/turns", headers=PLAYER).json()
        active = turns["active_generation"]
        assert active["id"] == generation
        collected = active["text"]
        offset = active["next_offset"]
        assert collected == provider.first
        assert offset == len(provider.first)
        assert [m["role"] for m in turns["messages"]] == ["user"]

        provider.release()
        deadline = monotonic() + 10
        while True:
            poll = client.get(
                f"{GENERATIONS}/{generation}",
                headers=PLAYER,
                params={"offset": offset, "wait": 2},
            ).json()
            collected += poll["text"]
            offset = poll["next_offset"]
            if poll["status"] not in ("pending", "streaming"):
                break
            assert monotonic() < deadline
    finally:
        provider.release()
        runner.join(10)

    # 이탈 중 생성분과 이후 분량이 순서대로 한 번씩만 모인다(AC10·AC13).
    assert poll["status"] == "completed"
    assert collected == provider.first + provider.rest
    assert collected == _generation(db, generation)["text"]
    assert provider.calls == 1
    assert count(db, "conversation_generations") == 1
    turns = client.get(f"{SESSIONS}/{session}/turns", headers=PLAYER).json()
    assert turns["active_generation"] is None
    assert turns["messages"][-1] == poll["assistant_message"]
    room = client.get(f"{SESSIONS}/{session}", headers=PLAYER).json()
    assert [m["body"] for m in room["messages"]] == ["비가 내린다.", "왔어, 민서?"]
    row = client.get(SESSIONS, headers=PLAYER).json()["sessions"][0]
    assert row["generating"] is False
    assert row["last_message_preview"].startswith("이탈 중에 만든 앞부분")


def test_chat_us_03_ac11_stale_generation_is_not_shown_as_generating(
    client, db, story, monkeypatch
):
    session = start(client, story)
    monkeypatch.setattr(worker, "submit", lambda job, *args: None)
    assert send(client, session, "멈춘 생성").status_code == 202
    assert client.get(SESSIONS, headers=PLAYER).json()["sessions"][0]["generating"] is True
    db.execute("UPDATE conversation_generations SET updated_at = now() - interval '1 hour'")
    row = client.get(SESSIONS, headers=PLAYER).json()["sessions"][0]
    assert row["id"] == session
    assert row["generating"] is False
