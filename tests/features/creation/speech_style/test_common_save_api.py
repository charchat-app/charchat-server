"""2번→7번 공통 원자 저장·완성 검증. 격리 PostgreSQL만 사용한다."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import psycopg
import pytest

from app.features.creation.common.errors import app_error
from app.features.creation.speech_style import repository
from app.features.creation.speech_style.schemas import DEFAULT_EXPRESSION


def url(story):
    return f"/api/v1/creation/stories/{story['id']}/creation"


def body(state="draft", text="", few="", mode="manual"):
    return {
        "expression": {
            **DEFAULT_EXPRESSION.model_dump(),
            "style_mode": "custom",
            "custom_style": text,
        },
        "fewshots": [
            {
                "id": None,
                "client_key": "g",
                "messages": [
                    {
                        "id": None,
                        "client_key": "m",
                        "speaker_kind": "narrator",
                        "character_id": None,
                        "body": few,
                    }
                ],
            }
        ],
        # 3번 연결 이후 완성 저장에는 유효한 인트로가 필요하다.
        "intros": [
            {
                "id": None,
                "client_key": "intro",
                "title": "첫 만남",
                "messages": [
                    {
                        "id": None,
                        "client_key": "intro-m",
                        "speaker_kind": "narrator",
                        "character_id": None,
                        "body": "*비가 내린다.*",
                    }
                ],
                "scene": {},
            }
        ],
        "relations": [],
        "affinities": [],
        "save_state": state,
        "mode": mode,
    }


def put(client, story, payload, key="common-one"):
    return client.put(url(story), json=payload, headers={"Idempotency-Key": key})


def complete(dsn, story):
    with psycopg.connect(dsn) as connection:
        connection.execute("UPDATE stories SET subtitle='한 줄 소개' WHERE id=%s", (story["id"],))
        connection.execute(
            "INSERT INTO characters(story_id,name,description,position) "
            "VALUES (%s,'하린','원래 상세',0)",
            (story["id"],),
        )


def current_payload(response, state=None):
    return {
        "expression": deepcopy(response["expression"]),
        "fewshots": deepcopy(response["fewshots"]),
        "intros": deepcopy(response["intros"]),
        "relations": deepcopy(response["relations"]),
        "affinities": deepcopy(response["affinities"]),
        "save_state": state or response["save_state"],
        "mode": "manual",
    }


def test_common_draft_preserves_overflow_raw_text_and_other_areas(context):
    client, _, story, _, dsn = context
    payload = body(text=" 😀\n" * 501, few=" **{{user}}**\n" * 201)
    result = put(client, story, payload)
    assert result.status_code == 200
    data = result.json()
    assert data["expression"]["custom_style"] == payload["expression"]["custom_style"]
    assert (
        data["fewshots"][0]["messages"][0]["body"] == payload["fewshots"][0]["messages"][0]["body"]
    )
    assert client.get(url(story)).json()["fewshots"] == [
        {
            **data["fewshots"][0],
            "client_key": f"group-{data['fewshots'][0]['id']}",
            "messages": [
                {
                    **data["fewshots"][0]["messages"][0],
                    "client_key": f"message-{data['fewshots'][0]['messages'][0]['id']}",
                }
            ],
        }
    ]
    with psycopg.connect(dsn) as connection:
        assert (
            connection.execute(
                "SELECT world_description FROM stories WHERE id=%s", (story["id"],)
            ).fetchone()[0]
            == "수정하면 안 되는 세계관"
        )


@pytest.mark.parametrize("state", ["private", "public"])
@pytest.mark.parametrize(
    ("field", "code"), [("custom", "CUSTOM_STYLE_TOO_LONG"), ("few", "FEWSHOTS_TOO_LONG")]
)
def test_both_completed_states_reject_overflow_atomically(context, state, field, code):
    client, _, story, _, dsn = context
    complete(dsn, story)
    before = client.get(url(story)).json()
    payload = body(
        state,
        text="😀" * (1001 if field == "custom" else 1000),
        few="가" * (2001 if field == "few" else 2000),
    )
    result = put(client, story, payload)
    assert result.status_code == 422
    assert result.json()["code"] == code
    assert client.get(url(story)).json() == before


def test_exact_unicode_boundaries_completed_edits_and_transitions(context):
    client, _, story, _, dsn = context
    complete(dsn, story)
    result = put(client, story, body("private", "😀" * 1000, "가" * 2000)).json()
    assert result["save_state"] == "private"
    payload = current_payload(result)
    payload["expression"]["custom_style"] = "  {{user}}\n*수정*  "
    edited = put(client, story, payload, "edited")
    assert edited.status_code == 200
    assert edited.json()["fewshots"][0]["id"] == result["fewshots"][0]["id"]
    assert put(client, story, {**payload, "save_state": "draft"}, "back-draft").status_code == 409
    assert put(client, story, {**payload, "mode": "auto"}, "auto-completed").status_code == 409
    public = put(client, story, {**payload, "save_state": "public"}, "publish")
    assert public.status_code == 200
    assert put(client, story, payload, "back-private").status_code == 409
    assert client.get(url(story)).json()["save_state"] == "public"


def test_completion_checks_stored_basic_values_and_empty_optional_fewshots(context):
    client, _, story, _, dsn = context
    payload = {**body("private"), "expression": DEFAULT_EXPRESSION.model_dump(), "fewshots": []}
    assert put(client, story, payload).json()["code"] == "BASIC_SETTINGS_INCOMPLETE"
    assert client.get(url(story)).json()["save_state"] == "draft"
    complete(dsn, story)
    assert put(client, story, payload, "complete-empty").status_code == 200


def test_common_retry_concurrency_old_replay_and_full_rollback(context, monkeypatch):
    client, _, story, _, _ = context
    payload = body(text="첫 원문", few="첫 본문")
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: put(client, story, payload), range(2)))
    assert all(result.status_code == 200 for result in responses)
    first = responses[0].json()
    assert responses[1].json() == first
    second_payload = current_payload(first)
    second_payload["expression"]["custom_style"] = "나중 원문"
    second_payload["fewshots"][0]["messages"][0]["body"] = "나중 본문"
    assert put(client, story, second_payload, "later").status_code == 200
    assert put(client, story, payload).json() == first
    before = client.get(url(story)).json()

    def fail(*_args, **_kwargs):
        raise app_error(503, "TEST_FAILURE", "검사용 실패")

    monkeypatch.setattr(repository, "remember_request", fail)
    assert put(client, story, {**second_payload, "fewshots": []}, "rollback").status_code == 503
    assert client.get(url(story)).json() == before


def test_auto_draft_owner_deleted_and_cross_endpoint_idempotency(context):
    client, _, story, _, dsn = context
    assert put(client, story, body(mode="auto")).status_code == 200
    assert (
        put(client, story, body("public", mode="auto"), "bad-auto").json()["code"]
        == "AUTO_SAVE_NOT_ALLOWED"
    )
    assert (
        client.put(
            f"/api/v1/creation/stories/{story['id']}/expression",
            json={"expression": DEFAULT_EXPRESSION.model_dump()},
            headers={"Idempotency-Key": "common-one"},
        ).status_code
        == 409
    )
    with psycopg.connect(dsn) as connection:
        foreign = connection.execute(
            "INSERT INTO stories(owner_id) VALUES (999999) RETURNING id"
        ).fetchone()[0]
    assert client.get(f"/api/v1/creation/stories/{foreign}/creation").status_code == 404
    with psycopg.connect(dsn) as connection:
        connection.execute(
            "UPDATE stories SET deleted_at=clock_timestamp() WHERE id=%s", (story["id"],)
        )
    for result in (client.get(url(story)), put(client, story, body(), "deleted")):
        assert result.status_code == 404
        assert result.json()["code"] == "STORY_NOT_FOUND"


def test_completed_rejection_lists_field_issues_with_over_count(context):
    client, _, story, _, dsn = context
    complete(dsn, story)
    with psycopg.connect(dsn) as connection:
        connection.execute(
            "INSERT INTO characters(story_id,name,description,position) "
            "VALUES (%s,'하 린','상세',1),(%s,' Doyun','상세',2)",
            (story["id"], story["id"]),
        )
    payload = body("private", text="😀" * 1003, few="가" * 2005)
    result = put(client, story, payload)
    assert result.status_code == 422
    assert result.headers["content-type"].startswith("application/problem+json")
    data = result.json()
    assert data["code"] == "CUSTOM_STYLE_TOO_LONG"
    issues = {(i["field"], i["code"]): i for i in data["issues"]}
    assert set(data["issues"][0]) == {"field", "code", "value", "message"}
    assert issues[("expression.custom_style", "CUSTOM_STYLE_TOO_LONG")]["value"] == "3"
    assert issues[("fewshots", "FEWSHOTS_TOO_LONG")]["value"] == "5"
    # common/names.py 규칙: 앞뒤 띄어쓰기는 형식 오류, 대소문자·띄어쓰기 무시 중복
    names = [i for i in data["issues"] if i["code"] == "INVALID_CHARACTER_NAME"]
    assert sorted(i["field"] for i in names) == [
        "characters[0].name",  # '하린'·'하 린' 중복
        "characters[1].name",
        "characters[2].name",  # ' Doyun' 앞 띄어쓰기
    ]
