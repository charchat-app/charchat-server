"""CREATE-US-03 인트로 → 7번 공통 저장·복원·완성 검증. 격리 PostgreSQL만 사용한다."""

from copy import deepcopy

import psycopg
import pytest

from app.features.creation.common.errors import app_error
from app.features.creation.speech_style import repository
from app.features.creation.speech_style.schemas import DEFAULT_EXPRESSION


def url(story):
    return f"/api/v1/creation/stories/{story['id']}/creation"


def message(key, body, kind="narrator", character_id=None, message_id=None):
    return {
        "id": message_id,
        "client_key": key,
        "speaker_kind": kind,
        "character_id": character_id,
        "body": body,
    }


def intro(key, title, messages, intro_id=None):
    return {
        "id": intro_id,
        "client_key": key,
        "title": title,
        "messages": messages,
    }


def payload(intros, state="draft", fewshots=None):
    return {
        "expression": DEFAULT_EXPRESSION.model_dump(),
        "fewshots": fewshots or [],
        "intros": intros,
        "save_state": state,
        "mode": "manual",
    }


def put(client, story, body, key):
    return client.put(url(story), json=body, headers={"Idempotency-Key": key})


def add_character(dsn, story, name, position):
    with psycopg.connect(dsn) as connection:
        return str(
            connection.execute(
                "INSERT INTO characters(story_id,name,description,position) "
                "VALUES (%s,%s,'상세',%s) RETURNING id",
                (story["id"], name, position),
            ).fetchone()[0]
        )


def complete_basics(dsn, story):
    with psycopg.connect(dsn) as connection:
        connection.execute("UPDATE stories SET subtitle='한 줄 소개' WHERE id=%s", (story["id"],))
    return add_character(dsn, story, "하린", 0)


def test_draft_keeps_incomplete_and_overflow_raw_text_and_restores_order(context):
    client, _, story, _, dsn = context
    hero = add_character(dsn, story, "하린", 0)
    raw = " **{{user}}**\n😀" * 400  # 2000자 초과 원문
    intros = [
        intro("i1", "", []),
        intro(
            "i2",
            "😀" * 31,
            [
                message("a", raw),
                message("b", "안녕, {{user}}.", "character", hero),
                message("c", "...", "user"),
                message("d", "또 같은 발화자", "character", hero),
            ],
        ),
    ]
    result = put(client, story, payload(intros), "draft-raw")
    assert result.status_code == 200, result.text
    saved = result.json()["intros"]
    assert [i["title"] for i in saved] == ["", "😀" * 31]
    assert [m["body"] for m in saved[1]["messages"]] == [
        raw,
        "안녕, {{user}}.",
        "...",
        "또 같은 발화자",
    ]
    assert saved[1]["messages"][1]["character_id"] == hero
    restored = client.get(url(story)).json()["intros"]
    assert [i["id"] for i in restored] == [i["id"] for i in saved]
    assert restored[1]["client_key"] == f"intro-{saved[1]['id']}"
    assert (
        restored[1]["messages"][0]["client_key"] == f"intro-message-{saved[1]['messages'][0]['id']}"
    )
    assert [m["body"] for m in restored[1]["messages"]] == [m["body"] for m in saved[1]["messages"]]
    assert put(client, story, payload([]), "draft-empty").status_code == 200
    assert client.get(url(story)).json()["intros"] == []


def test_reorder_edit_and_delete_keep_ids_and_first_is_default(context):
    client, _, story, _, _ = context
    first = put(
        client,
        story,
        payload(
            [
                intro("i1", "기본", [message("a", "1-a"), message("b", "1-b")]),
                intro("i2", "둘째", [message("c", "2-a")]),
                intro("i3", "셋째", [message("d", "3-a")]),
            ]
        ),
        "first",
    ).json()["intros"]
    reordered = deepcopy([first[1], first[0], first[2]])
    reordered[1]["messages"].reverse()
    reordered[1]["messages"][0]["body"] = "1-b 수정"
    reordered[1]["title"] = "기본 수정"
    del reordered[2]  # 셋째 인트로 삭제
    del reordered[0]["messages"][0]  # 둘째 인트로의 유일한 말풍선 삭제
    result = put(client, story, payload(reordered), "second")
    assert result.status_code == 200, result.text
    restored = client.get(url(story)).json()["intros"]
    assert [i["id"] for i in restored] == [first[1]["id"], first[0]["id"]]
    assert restored[0]["messages"] == []
    assert restored[1]["title"] == "기본 수정"
    assert [m["id"] for m in restored[1]["messages"]] == [
        first[0]["messages"][1]["id"],
        first[0]["messages"][0]["id"],
    ]
    assert [m["body"] for m in restored[1]["messages"]] == ["1-b 수정", "1-a"]
    # 기본 인트로 삭제 → 남은 첫 번째가 기본
    only = deepcopy(restored[1:])
    assert put(client, story, payload(only), "third").status_code == 200
    assert client.get(url(story)).json()["intros"][0]["id"] == first[0]["id"]


def test_rejects_cross_intro_move_foreign_speaker_and_more_than_five(context):
    client, _, story, _, dsn = context
    saved = put(
        client,
        story,
        payload([intro("i1", "A", [message("a", "본문")]), intro("i2", "B", [])]),
        "base",
    ).json()["intros"]
    before = client.get(url(story)).json()
    moved = deepcopy(saved)
    moved[1]["messages"].append(moved[0]["messages"].pop())
    result = put(client, story, payload(moved), "move")
    assert result.status_code == 422
    assert result.json()["code"] == "MESSAGE_INTRO_CHANGED"
    with psycopg.connect(dsn) as connection:
        other = connection.execute(
            "INSERT INTO stories(owner_id) VALUES (999998) RETURNING id"
        ).fetchone()[0]
        foreign = connection.execute(
            "INSERT INTO characters(story_id,name,description,position) "
            "VALUES (%s,'남','상세',0) RETURNING id",
            (other,),
        ).fetchone()[0]
    bad = put(
        client,
        story,
        payload([intro("x", "A", [message("x1", "본문", "character", str(foreign))])]),
        "foreign",
    )
    assert bad.json()["code"] == "INVALID_SPEAKER"
    six = [intro(f"k{n}", f"제목{n}", []) for n in range(6)]
    assert put(client, story, payload(six), "six").json()["code"] == "VALIDATION_ERROR"
    assert client.get(url(story)).json() == before


@pytest.mark.parametrize("state", ["private", "public"])
@pytest.mark.parametrize(
    ("intros", "code"),
    [
        ([], "INTRO_REQUIRED"),
        ([intro("i", "  ", [message("m", "본문")])], "INTRO_TITLE_REQUIRED"),
        ([intro("i", None, [message("m", "본문")])], "INTRO_TITLE_REQUIRED"),
        ([intro("i", "😀" * 31, [message("m", "본문")])], "INTRO_TITLE_TOO_LONG"),
        ([intro("i", "제목", [])], "INTRO_BODY_REQUIRED"),
        ([intro("i", "제목", [message("m", " \n ")])], "INTRO_BODY_REQUIRED"),
        (
            [intro("i", "제목", [message("m", "가" * 1500), message("n", "😀" * 501)])],
            "INTRO_TOO_LONG",
        ),
    ],
)
def test_completed_save_rejects_invalid_intros_atomically(context, state, intros, code):
    client, _, story, _, dsn = context
    complete_basics(dsn, story)
    before = client.get(url(story)).json()
    result = put(client, story, payload(intros, state), "invalid")
    assert result.status_code == 422
    assert result.json()["code"] == code
    assert client.get(url(story)).json() == before


def test_completed_save_accepts_per_intro_boundaries_without_sum_limit(context):
    client, _, story, _, dsn = context
    complete_basics(dsn, story)
    intros = [
        intro("i1", "😀" * 30, [message("a", "가" * 1000), message("b", "*" * 1000)]),
        intro("i2", "둘째", [message("c", "😀" * 2000)]),
    ]
    result = put(client, story, payload(intros, "private"), "bounds")
    assert result.status_code == 200, result.text
    assert result.json()["save_state"] == "private"
    assert [i["title"] for i in client.get(url(story)).json()["intros"]] == ["😀" * 30, "둘째"]


def test_character_delete_removes_only_their_intro_messages(context):
    client, _, story, _, dsn = context
    hero = add_character(dsn, story, "하린", 0)
    other = add_character(dsn, story, "도윤", 1)
    put(
        client,
        story,
        payload(
            [
                intro(
                    "i1",
                    "A",
                    [
                        message("a", "하린이 웃는다.", "narrator"),
                        message("b", "하린 대사", "character", hero),
                        message("c", "도윤 대사", "character", other),
                    ],
                ),
                intro("i2", "B", [message("d", "하린 둘째", "character", hero)]),
            ]
        ),
        "people",
    )
    with psycopg.connect(dsn) as connection:
        connection.execute("DELETE FROM characters WHERE id=%s", (int(hero),))
    restored = client.get(url(story)).json()["intros"]
    assert [m["body"] for m in restored[0]["messages"]] == ["하린이 웃는다.", "도윤 대사"]
    assert restored[1]["messages"] == []


def test_intro_write_rolls_back_with_common_save(context, monkeypatch):
    client, _, story, _, _ = context
    put(client, story, payload([intro("i1", "원래", [message("a", "원래 본문")])]), "keep")
    before = client.get(url(story)).json()

    def fail(*_args, **_kwargs):
        raise app_error(503, "TEST_FAILURE", "검사용 실패")

    monkeypatch.setattr(repository, "remember_request", fail)
    assert put(client, story, payload([]), "rollback").status_code == 503
    assert client.get(url(story)).json() == before
