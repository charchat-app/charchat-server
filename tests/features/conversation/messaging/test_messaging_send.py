"""CHAT-US-02 메시지 전송·검증·중복 방지·이력 조회."""

from concurrent.futures import ThreadPoolExecutor
from time import monotonic, sleep

import psycopg
import pytest
from messaging_helpers import (
    AUTHOR,
    GENERATIONS,
    PLAYER,
    SESSIONS,
    send,
    session_characters,
    start_session,
    user_part,
)

from app.core.config import settings


def count(db, table):
    return db.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]


def test_chat_us_02_ac01_ac02_mixed_speakers_saved_in_order_and_reply_stored(client, db, session):
    chars = session_characters(client, session)
    parts = [
        user_part("*문을 연다.* 늦었어?"),
        {"speaker_kind": "narrator", "character_id": None, "body": "천둥이 친다."},
        {"speaker_kind": "character", "character_id": chars["Bea"], "body": "**왔다!**"},
        user_part("미안."),
    ]
    response = send(client, session, parts)
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "pending"
    assert body["session_id"] == session
    assert body["assistant_message"] is None
    assert body["retryable"] is False

    turns = client.get(f"{SESSIONS}/{session}/turns", headers=PLAYER).json()
    user_message, assistant = turns["messages"]
    assert user_message["id"] == body["user_message_id"]
    assert user_message["role"] == "user"
    assert [
        (p["speaker_kind"], p["character_id"], p["speaker_name"], p["body"])
        for p in user_message["parts"]
    ] == [
        ("user", None, "민서", parts[0]["body"]),
        ("narrator", None, None, "천둥이 친다."),
        ("character", chars["Bea"], "Bea", "**왔다!**"),
        ("user", None, "민서", "미안."),
    ]
    # AC09: 완료 응답은 발화자·본문 순서를 지켜 저장된다(fake 공급자는 첫 캐릭터 + 나레이터).
    assert assistant["role"] == "assistant"
    assert [(p["speaker_kind"], p["speaker_name"]) for p in assistant["parts"]] == [
        ("character", "도경"),
        ("narrator", None),
    ]
    assert assistant["parts"][0]["character_id"] == chars["도경"]
    assert turns["active_generation"] is None
    assert turns["last_failed_generation"] is None


def test_chat_us_02_ac09_ac12_completed_generation_is_billable_and_linked(client, db, session):
    response = send(client, session, [user_part("안녕")]).json()
    done = client.get(f"{GENERATIONS}/{response['id']}", headers=PLAYER).json()
    assert done["status"] == "completed"
    assert done["error_code"] is None
    assert done["text"].startswith("@도경: (가짜 응답)")
    assert done["next_offset"] == len(done["text"])
    assert done["assistant_message"]["parts"][0]["body"].startswith("(가짜 응답)")
    assert "가짜 모델(gpt-6-luna)" in done["text"]
    row = db.execute(
        """SELECT g.billable, g.assistant_message_id, m.name AS model, msg.status
           FROM conversation_generations g JOIN models m ON m.id = g.model_id
           JOIN conversation_messages msg
             ON msg.session_id = g.session_id AND msg.id = g.assistant_message_id"""
    ).fetchone()
    assert row == {
        "billable": True,
        "assistant_message_id": done["assistant_message"]["id"],
        "model": "루나",
        "status": "completed",
    }


def test_kan16_ac20_ac21_default_model_applied_and_shown(client, db, session):
    # 세션 시작 때 기본 모델(CHAT_DEFAULT_MODEL_NAME=루나)이 selected_model_id로 고정된다.
    assert client.get(f"{SESSIONS}/{session}", headers=PLAYER).json()["model"] == "루나"
    assert client.get(f"{SESSIONS}/{session}/turns", headers=PLAYER).json()["model"] == "루나"
    send(client, session, [user_part("안녕")])
    assert client.get(f"{SESSIONS}/{session}", headers=PLAYER).json()["model"] == "루나"
    row = db.execute(
        """SELECT m.name FROM conversation_sessions cs
           JOIN models m ON m.id = cs.selected_model_id"""
    ).fetchone()
    assert row == {"name": "루나"}


def test_kan16_ac20_default_model_name_setting_selects_model(client, db, saved_story, monkeypatch):
    monkeypatch.setattr(settings, "chat_default_model_name", "테라")
    session = start_session(client, saved_story[0], saved_story[1])
    assert client.get(f"{SESSIONS}/{session}", headers=PLAYER).json()["model"] == "테라"
    send(client, session, [user_part("안녕")])
    done = client.get(f"{SESSIONS}/{session}/turns", headers=PLAYER).json()
    assert done["model"] == "테라"
    assert "가짜 모델(gpt-6.1-terra)" in done["messages"][1]["parts"][-1]["body"]


def test_kan16_ac20_missing_default_model_rejects_session_start(
    client, db, saved_story, monkeypatch
):
    monkeypatch.setattr(settings, "chat_default_model_name", "없는 모델")
    response = client.post(
        f"/api/v1/conversation/stories/{saved_story[0]}/sessions",
        headers=PLAYER,
        json={
            "intro_id": saved_story[1],
            "profile_kind": "preset",
            "profile_id": 1,
            "play_name": "",
        },
    )
    assert response.status_code == 503
    assert response.json()["code"] == "CONVERSATION_MODEL_NOT_CONFIGURED"
    assert count(db, "conversation_sessions") == 0


def test_chat_us_02_parts_stored_in_team_tables_with_speakers(client, db, session):
    """이용자 부분은 고른 발화자 그대로, AI 응답은 파싱한 발화자별 부분으로 저장된다."""
    chars = session_characters(client, session)
    parts = [
        user_part("나는 유저"),
        {"speaker_kind": "character", "character_id": chars["Bea"], "body": "나는 Bea"},
        {"speaker_kind": "narrator", "character_id": None, "body": "나레이션"},
    ]
    generation = send(client, session, parts).json()
    rows = db.execute(
        """SELECT m.position AS message_position, m.role, m.status, p.position,
                  p.speaker_kind, p.session_character_id, p.body
           FROM conversation_messages m
           JOIN conversation_message_parts p ON p.session_id = m.session_id AND p.message_id = m.id
           WHERE m.session_id = %s ORDER BY m.position, p.position""",
        (session,),
    ).fetchall()
    assert [
        (
            r["message_position"],
            r["role"],
            r["status"],
            r["position"],
            r["speaker_kind"],
            r["session_character_id"],
        )
        for r in rows
    ] == [
        (0, "user", "completed", 0, "user", None),
        (0, "user", "completed", 1, "character", chars["Bea"]),
        (0, "user", "completed", 2, "narrator", None),
        (1, "assistant", "completed", 0, "character", chars["도경"]),
        (1, "assistant", "completed", 1, "narrator", None),
    ]
    assert [r["body"] for r in rows[:3]] == ["나는 유저", "나는 Bea", "나레이션"]
    assert (
        generation["user_message_id"]
        == db.execute("SELECT id FROM conversation_messages WHERE position = 0").fetchone()["id"]
    )


@pytest.mark.parametrize(
    "parts",
    [[], [user_part("")], [user_part("  \n\t")], [user_part(" "), user_part("\n")]],
)
def test_chat_us_02_ac04_empty_input_rejected_without_generation(client, db, session, parts):
    response = send(client, session, parts)
    assert response.status_code == 422
    assert response.json()["code"] == "MESSAGE_EMPTY"
    assert count(db, "conversation_messages") == 0
    assert count(db, "conversation_generations") == 0


def test_chat_us_02_ac04_total_limit_counts_all_parts_spaces_newlines_and_markup(
    client, db, session
):
    chars = session_characters(client, session)
    # 이모지·한글도 코드 포인트 하나로 센다. 합계 정확히 5000자는 허용한다.
    exact = [
        user_part("*" + "가" * 1998 + "*"),
        {"speaker_kind": "narrator", "character_id": None, "body": " \n" * 500},
        {"speaker_kind": "character", "character_id": chars["도경"], "body": "😀" * 2000},
    ]
    assert sum(len(p["body"]) for p in exact) == 5000
    assert send(client, session, exact, request_id="exact-5000").status_code == 202

    over = [user_part("**" + "a" * 4997), user_part("\n")]
    assert sum(len(p["body"]) for p in over) == 5000
    over[1]["body"] = "\n\n"
    response = send(client, session, over, request_id="over-5001")
    assert response.status_code == 422
    assert response.json()["code"] == "MESSAGE_TOO_LONG"
    assert count(db, "conversation_generations") == 1
    assert count(db, "conversation_messages") == 2  # 앞의 user + assistant


@pytest.mark.parametrize(
    "part",
    [
        {"speaker_kind": "character", "character_id": None, "body": "누구?"},
        {"speaker_kind": "character", "character_id": 9999, "body": "다른 세션"},
        {"speaker_kind": "user", "character_id": 1, "body": "섞임"},
        {"speaker_kind": "narrator", "character_id": 1, "body": "섞임"},
    ],
)
def test_chat_us_02_ac01_invalid_speaker_rejected(client, db, session, part):
    response = send(client, session, [part])
    assert response.status_code == 422
    assert response.json()["code"] == "MESSAGE_INVALID_SPEAKER"
    assert count(db, "conversation_generations") == 0


def test_chat_us_02_ac01_other_session_character_id_rejected(client, db, saved_story, session):
    from messaging_helpers import start_session

    story, intro, _ = saved_story
    other = start_session(client, story, intro, headers=AUTHOR)
    other_char = session_characters(client, other, headers=AUTHOR)["도경"]
    assert other_char not in session_characters(client, session).values()
    part = {"speaker_kind": "character", "character_id": other_char, "body": "침입"}
    assert send(client, session, [part]).json()["code"] == "MESSAGE_INVALID_SPEAKER"


def test_chat_us_02_nul_rejected_at_request_validation(client, db, session):
    response = send(client, session, [user_part("앞\0뒤")])
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert count(db, "conversation_messages") == 0


@pytest.mark.parametrize("request_id", ["short", "has space 123", "a" * 65, "한글요청아이디입니다"])
def test_chat_us_02_request_id_format_validated(client, db, session, request_id):
    response = send(client, session, [user_part("hi")], request_id=request_id)
    assert response.status_code == 422
    assert count(db, "conversation_generations") == 0


def test_chat_us_02_other_owner_gets_404_everywhere(client, db, session):
    generation = send(client, session, [user_part("hi")]).json()["id"]
    assert send(client, session, [user_part("hi")], headers=AUTHOR).status_code == 404
    assert client.get(f"{SESSIONS}/{session}/turns", headers=AUTHOR).status_code == 404
    assert client.get(f"{GENERATIONS}/{generation}", headers=AUTHOR).status_code == 404
    retry = client.post(
        f"{GENERATIONS}/{generation}/retry", headers=AUTHOR, json={"request_id": "retry-0001"}
    )
    assert retry.status_code == 404
    assert client.get(f"{SESSIONS}/99999/turns", headers=PLAYER).status_code == 404


def test_chat_us_02_deleted_story_session_is_read_only(client, db, saved_story, session):
    send(client, session, [user_part("마지막")])
    db.execute("UPDATE stories SET deleted_at = now() WHERE id = %s", (saved_story[0],))
    response = send(client, session, [user_part("더")], request_id="after-delete")
    assert response.status_code == 409
    assert response.json()["code"] == "CONVERSATION_READ_ONLY"
    # 이력은 읽을 수 있다.
    turns = client.get(f"{SESSIONS}/{session}/turns", headers=PLAYER)
    assert turns.status_code == 200
    assert len(turns.json()["messages"]) == 2
    assert count(db, "conversation_generations") == 1


def test_chat_us_02_ac10_same_request_id_returns_existing_generation(client, db, session):
    first = send(client, session, [user_part("한 번만")])
    again = send(client, session, [user_part("다른 내용이어도")])
    assert first.status_code == again.status_code == 202
    assert again.json()["id"] == first.json()["id"]
    assert again.json()["status"] == "completed"
    assert count(db, "conversation_generations") == 1
    assert count(db, "conversation_messages") == 2


def _hold_session_lock(db, db_url, session, requests_fn, expected_waiters):
    """세션 행 잠금을 쥔 채 동시 요청을 잠금 앞에 세운 뒤 한꺼번에 풀어 준다."""
    with ThreadPoolExecutor(max_workers=expected_waiters) as workers:
        with psycopg.connect(db_url) as gate:
            gate.execute(
                "SELECT id FROM conversation_sessions WHERE id = %s FOR UPDATE", (session,)
            )
            futures = [workers.submit(fn) for fn in requests_fn]
            deadline = monotonic() + 5
            while True:
                waiting = db.execute(
                    """SELECT count(*) AS n FROM pg_stat_activity
                       WHERE datname = current_database() AND wait_event_type = 'Lock'
                         AND query LIKE '%FROM conversation_sessions cs%FOR UPDATE%'"""
                ).fetchone()["n"]
                if waiting == expected_waiters:
                    break
                assert monotonic() < deadline, "요청이 세션 잠금에 도달하지 않았습니다."
                sleep(0.01)
        return [future.result(timeout=10) for future in futures]


def test_chat_us_02_ac10_concurrent_identical_posts_create_one_generation(
    client, db, db_url, session
):
    results = _hold_session_lock(
        db,
        db_url,
        session,
        [lambda: send(client, session, [user_part("동시")]) for _ in range(2)],
        2,
    )
    assert [r.status_code for r in results] == [202, 202]
    assert results[0].json()["id"] == results[1].json()["id"]
    assert count(db, "conversation_generations") == 1
    assert count(db, "conversation_messages") == 2


def test_chat_us_02_ac06_ac10_second_message_while_active_is_rejected(
    client, db, db_url, session, monkeypatch
):
    from app.features.conversation.messaging import worker

    # 생성 작업을 시작하지 않아 첫 요청이 진행 중(pending)으로 남게 한다.
    queued = []
    monkeypatch.setattr(worker, "submit", lambda job, *args: queued.append(args))
    first = send(client, session, [user_part("첫 번째")], request_id="first-0001")
    assert first.json()["status"] == "pending"
    second = send(client, session, [user_part("두 번째")], request_id="second-001")
    assert second.status_code == 409
    assert second.json()["code"] == "GENERATION_IN_PROGRESS"
    assert second.json()["active_generation_id"] == first.json()["id"]
    assert count(db, "conversation_messages") == 2  # 첫 요청의 user + pending assistant

    turns = client.get(f"{SESSIONS}/{session}/turns", headers=PLAYER).json()
    assert turns["active_generation"]["id"] == first.json()["id"]
    assert turns["active_generation"]["status"] == "pending"
    assert [m["role"] for m in turns["messages"]] == ["user"]


def test_chat_us_02_ac10_concurrent_different_requests_allow_one_active(
    client, db, db_url, session, monkeypatch
):
    from app.features.conversation.messaging import worker

    monkeypatch.setattr(worker, "submit", lambda job, *args: None)
    results = _hold_session_lock(
        db,
        db_url,
        session,
        [
            lambda: send(client, session, [user_part("A")], request_id="request-a1"),
            lambda: send(client, session, [user_part("B")], request_id="request-b1"),
        ],
        2,
    )
    assert sorted(r.status_code for r in results) == [202, 409]
    assert count(db, "conversation_generations") == 1
    assert count(db, "conversation_messages") == 2  # 첫 요청의 user + pending assistant


def test_chat_us_02_active_index_blocks_second_active_row_in_db(client, db, session, monkeypatch):
    from app.features.conversation.messaging import worker

    monkeypatch.setattr(worker, "submit", lambda job, *args: None)
    send(client, session, [user_part("A")], request_id="request-a1")
    message = db.execute("SELECT id FROM conversation_messages WHERE role = 'user'").fetchone()[
        "id"
    ]
    extra = db.execute(
        """INSERT INTO conversation_messages (session_id, position, role, status)
           VALUES (%s, 99, 'assistant', 'pending') RETURNING id""",
        (session,),
    ).fetchone()["id"]
    with pytest.raises(psycopg.errors.UniqueViolation) as exc:
        db.execute(
            """INSERT INTO conversation_generations
                   (session_id, request_id, user_message_id, assistant_message_id, model_id,
                    status)
               VALUES (%s, 'manual-1', %s, %s, (SELECT min(id) FROM models), 'streaming')""",
            (session, message, extra),
        )
    assert exc.value.diag.constraint_name == "conversation_generations_one_active_idx"


def test_chat_us_02_ac11_turns_keep_history_order_across_turns(client, db, session):
    for index in range(3):
        send(client, session, [user_part(f"입력 {index}")], request_id=f"turn-000{index}")
    messages = client.get(f"{SESSIONS}/{session}/turns", headers=PLAYER).json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"] * 3
    assert [m["parts"][0]["body"] for m in messages[::2]] == ["입력 0", "입력 1", "입력 2"]
    rows = db.execute("SELECT position, role FROM conversation_messages ORDER BY id").fetchall()
    # 세션마다 0부터 늘어나는 순서. 인트로는 메시지로 복사하지 않는다.
    assert [(row["position"], row["role"]) for row in rows] == [
        (index, role) for index, role in enumerate(["user", "assistant"] * 3)
    ]
