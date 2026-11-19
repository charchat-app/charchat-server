"""CREATE-US-02 AC01~06·20~21 저장/복원의 실제 PostgreSQL 통합검사."""

from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest

from app.core.config import settings
from app.features.creation.common.errors import app_error
from app.features.creation.speech_style import repository

DEFAULT = {
    "point_of_view": "third_person",
    "tense": "past",
    "composition": "balanced",
    "response_length": "normal",
    "style_mode": "none",
    "preset_styles": [],
    "custom_style": None,
}


def endpoint(story):
    return f"/api/v1/creation/stories/{story['id']}/expression"


def put(client, url, fields=None, key="request-one", **extra):
    return client.put(
        url, json={"expression": fields or DEFAULT, **extra}, headers={"Idempotency-Key": key}
    )


def test_requests_without_dev_user_header_are_rejected(client):
    # 다른 이름의 소유자 헤더나 잘못된 값으로는 사용자를 정할 수 없다.
    for headers in [{}, {"X-Owner-Id": "1"}, {"X-User-Id": "0"}, {"X-User-Id": "abc"}]:
        response = client.get("/api/v1/creation/stories/1/expression", headers=headers)
        assert response.status_code == 401
        assert response.headers["content-type"].startswith("application/problem+json")
        assert response.json()["code"] == "UNAUTHENTICATED"


def test_get_default_is_read_only_CREATE_US_02_AC01(context):
    client, _, story, _, dsn = context
    response = client.get(endpoint(story))
    assert response.status_code == 200
    assert response.json()["expression"] == DEFAULT
    with psycopg.connect(dsn) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM story_expression_settings WHERE story_id=%s",
                (story["id"],),
            ).fetchone()[0]
            == 0
        )


def test_save_restore_and_replay_after_later_save_CREATE_US_02_AC20_AC21(context):
    client, _, story, _, dsn = context
    url = endpoint(story)
    fields = {
        **DEFAULT,
        "style_mode": "custom",
        "custom_style": "  {{user}}\n" + "가" * 1001 + "  ",
    }
    first = put(client, url, fields)
    assert first.status_code == 200
    assert first.json()["expression"] == fields
    assert client.get(url).json()["expression"] == fields
    second = {**DEFAULT, "tense": "present"}
    assert put(client, url, second, "request-two").status_code == 200
    assert put(client, url, fields).json() == first.json()
    assert client.get(url).json()["expression"] == second
    conflict = put(client, url, second)
    assert conflict.status_code == 409
    with psycopg.connect(dsn) as connection:
        assert (
            connection.execute(
                "SELECT world_description FROM stories WHERE id=%s", (story["id"],)
            ).fetchone()[0]
            == "수정하면 안 되는 세계관"
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"composition": "unknown"},
        {"style_mode": "preset", "preset_styles": ["plain", "warm", "dynamic"]},
        {"style_mode": "preset", "preset_styles": ["plain", "plain"]},
        {"style_mode": "preset", "preset_styles": ["plain"], "custom_style": "개인 입력"},
        {"style_mode": "none", "custom_style": "개인 입력"},
    ],
)
def test_invalid_expression_is_problem_without_echoing_input(context, changes):
    client, _, story, _, _ = context
    response = put(client, endpoint(story), {**DEFAULT, **changes})
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert "개인 입력" not in response.text
    assert client.get(endpoint(story)).json()["expression"] == DEFAULT


def test_owner_and_deleted_story_are_protected(context):
    client, _, story, owner_id, dsn = context
    with psycopg.connect(dsn) as connection:
        other = connection.execute(
            "INSERT INTO stories(owner_id) VALUES (%s) RETURNING id", (owner_id + 10000,)
        ).fetchone()[0]
    for method in ("get", "put"):
        response = (
            client.get(f"/api/v1/creation/stories/{other}/expression")
            if method == "get"
            else put(
                client,
                f"/api/v1/creation/stories/{other}/expression",
            )
        )
        assert response.status_code == 404
    with psycopg.connect(dsn) as connection:
        connection.execute(
            "UPDATE stories SET deleted_at=clock_timestamp() WHERE id=%s", (story["id"],)
        )
    # 기본 정보 API와 같이 삭제된 스토리는 404 STORY_NOT_FOUND로 숨긴다.
    for response in (client.get(endpoint(story)), put(client, endpoint(story))):
        assert response.status_code == 404
        assert response.json()["code"] == "STORY_NOT_FOUND"


def test_completed_story_requires_common_save(context):
    client, _, story, _, dsn = context
    with psycopg.connect(dsn) as connection:
        connection.execute("UPDATE stories SET save_state='private' WHERE id=%s", (story["id"],))
    assert client.get(endpoint(story)).json()["save_state"] == "private"
    assert put(client, endpoint(story)).json()["code"] == "FULL_SAVE_REQUIRED"


def test_failed_receipt_rolls_back_expression_and_saved_at(context, monkeypatch):
    client, _, story, _, dsn = context

    def fail(*_args):
        raise app_error(503, "TEST_FAILURE", "가상 실패")

    monkeypatch.setattr(repository, "remember_request", fail)
    assert put(client, endpoint(story), {**DEFAULT, "tense": "present"}).status_code == 503
    with psycopg.connect(dsn) as connection:
        assert (
            connection.execute(
                "SELECT saved_at FROM stories WHERE id=%s", (story["id"],)
            ).fetchone()[0]
            == story["saved_at"]
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM story_expression_settings WHERE story_id=%s", (story["id"],)
            ).fetchone()[0]
            == 0
        )


def test_same_request_concurrently_commits_once(context):
    client, _, story, owner_id, dsn = context
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: put(client, endpoint(story)), range(2)))
    assert [response.status_code for response in results] == [200, 200]
    assert results[0].json() == results[1].json()
    with psycopg.connect(dsn) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM creation_write_requests WHERE owner_id=%s", (owner_id,)
            ).fetchone()[0]
            == 1
        )


def test_request_contract_and_cors(context):
    client, _, story, _, _ = context
    assert client.put(endpoint(story), json={"expression": DEFAULT}).status_code == 422
    assert put(client, endpoint(story), owner_id=999).status_code == 422
    assert client.get("/api/v1/creation/stories/9223372036854775808/expression").status_code == 422
    origin = settings.cors_origins[0]
    response = client.options(
        endpoint(story),
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "content-type,idempotency-key",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin


def test_story_id_is_number_in_responses(context):
    client, _, story, _, _ = context
    assert client.get(endpoint(story)).json()["story_id"] == story["id"]
    assert put(client, endpoint(story)).json()["story_id"] == story["id"]


def test_custom_style_with_nul_is_rejected_without_echo(context):
    client, _, story, _, _ = context
    fields = {**DEFAULT, "style_mode": "custom", "custom_style": "개인 입력\x00"}
    response = put(client, endpoint(story), fields)
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert "개인 입력" not in response.text
    assert client.get(endpoint(story)).json()["expression"] == DEFAULT
