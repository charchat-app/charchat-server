"""CREATE-US-02 AC08~21 퓨샷 CRUD·정렬·원문·참조·롤백 통합검사."""

from concurrent.futures import ThreadPoolExecutor

import psycopg

from app.features.creation.common.errors import app_error
from app.features.creation.speech_style import repository


def url(story):
    return f"/api/v1/creation/stories/{story['id']}/fewshots"


def message(key, body="본문", kind="user", character=None, identity=None):
    return {
        "id": identity,
        "client_key": key,
        "speaker_kind": kind,
        "character_id": character,
        "body": body,
    }


def group(key, messages=None, identity=None):
    return {"id": identity, "client_key": key, "messages": messages or []}


def put(client, story, groups, key="fewshots-one"):
    return client.put(url(story), json={"fewshots": groups}, headers={"Idempotency-Key": key})


def character(dsn, story_id, name="하린"):
    with psycopg.connect(dsn) as connection:
        return connection.execute(
            """INSERT INTO characters(story_id,name,description,position)
               VALUES (%s,%s,%s,0) RETURNING id""",
            (story_id, name, "가상 캐릭터"),
        ).fetchone()[0]


def test_empty_read_does_not_create_rows(context):
    client, _, story, _, dsn = context
    cid = character(dsn, story["id"])
    data = client.get(url(story)).json()
    assert data["fewshots"] == []
    assert data["characters"][0]["id"] == cid
    with psycopg.connect(dsn) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM fewshots WHERE story_id=%s", (story["id"],)
            ).fetchone()[0]
            == 0
        )


def test_add_edit_delete_and_both_orders_preserve_ids_and_raw_text(context):
    client, _, story, _, dsn = context
    cid = character(dsn, story["id"])
    raw = "  {{user}}\n*서술* **강조** " + "가" * 2001
    first = put(
        client,
        story,
        [
            group(
                "g-one",
                [message("m-one", raw, "character", cid), message("m-two", None, "narrator")],
            ),
            group("g-two", [message("m-three", "연속 유저"), message("m-four", "연속 유저")]),
        ],
    )
    assert first.status_code == 200
    groups = first.json()["fewshots"]
    original_ids = [[g["id"], [m["id"] for m in g["messages"]]] for g in groups]
    groups[0]["messages"].reverse()
    groups[0]["messages"][0]["body"] = "수정 본문"
    groups.reverse()
    changed = put(client, story, groups, "fewshots-two")
    assert changed.status_code == 200
    restored = client.get(url(story)).json()["fewshots"]
    assert restored[0]["id"] == original_ids[1][0]
    assert [m["id"] for m in restored[1]["messages"]] == list(reversed(original_ids[0][1]))
    assert restored[1]["messages"][1]["body"] == raw
    restored[0]["messages"].pop()
    assert put(client, story, restored, "delete-message").status_code == 200
    assert len(client.get(url(story)).json()["fewshots"][0]["messages"]) == 1
    assert put(client, story, [restored[1]], "delete-group").status_code == 200
    assert len(client.get(url(story)).json()["fewshots"]) == 1
    assert put(client, story, [], "delete-all").status_code == 200
    assert client.get(url(story)).json()["fewshots"] == []
    with psycopg.connect(dsn) as connection:
        assert (
            connection.execute(
                "SELECT world_description FROM stories WHERE id=%s", (story["id"],)
            ).fetchone()[0]
            == "수정하면 안 되는 세계관"
        )


def test_reference_checks_unknown_id_cross_story_and_parent_change(context):
    client, _, story, owner, dsn = context
    with psycopg.connect(dsn) as connection:
        other = connection.execute(
            "INSERT INTO stories(owner_id) VALUES (%s) RETURNING id", (owner,)
        ).fetchone()[0]
    foreign_character = character(dsn, other)
    assert (
        put(
            client,
            story,
            [group("g", [message("m", kind="character", character=foreign_character)])],
        ).status_code
        == 422
    )
    assert (
        put(client, story, [group("g", [message("m", identity=9223372036854775807)])]).status_code
        == 422
    )
    assert put(client, story, [group("g", identity=9223372036854775807)]).status_code == 422
    response = put(client, story, [group("ga", [message("ma")]), group("gb")], "valid")
    groups = response.json()["fewshots"]
    groups[1]["messages"] = groups[0]["messages"]
    groups[0]["messages"] = []
    error = put(client, story, groups, "move-parent")
    assert error.json()["code"] == "MESSAGE_GROUP_CHANGED"
    assert len(client.get(url(story)).json()["fewshots"][0]["messages"]) == 1


def test_duplicate_keys_ids_and_invalid_speaker_are_rejected(context):
    client, _, story, _, _ = context
    cases = [
        [group("g"), group("g")],
        [group("g", [message("g")])],
        [group("g", [message("m", kind="character")])],
        [group("g", [message("m", "개인 원문\x00")])],
    ]
    for groups in cases:
        response = put(client, story, groups)
        assert response.status_code == 422
        assert "개인 원문" not in response.text
    groups = put(client, story, [group("g", [message("m")])], "create").json()["fewshots"]
    assert (
        put(client, story, [groups[0], {**groups[0], "client_key": "g2"}], "dupe-id").status_code
        == 422
    )


def test_idempotency_concurrency_and_replay_do_not_recreate_or_overwrite(context):
    client, _, story, owner, dsn = context
    payload = [group("g", [message("m")])]
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: put(client, story, payload), range(2)))
    assert responses[0].status_code == responses[1].status_code == 200
    assert responses[0].json() == responses[1].json()
    newer = responses[0].json()["fewshots"]
    newer[0]["messages"][0]["body"] = "나중 저장"
    assert put(client, story, newer, "newer").status_code == 200
    assert put(client, story, payload).json() == responses[0].json()
    assert client.get(url(story)).json()["fewshots"][0]["messages"][0]["body"] == "나중 저장"
    assert put(client, story, [], "fewshots-one").status_code == 409
    with psycopg.connect(dsn) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM fewshots WHERE story_id=%s", (story["id"],)
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM creation_write_requests WHERE owner_id=%s", (owner,)
            ).fetchone()[0]
            == 2
        )


def test_rollback_preserves_deleted_rows_order_and_saved_at(context, monkeypatch):
    client, _, story, _, _ = context
    assert put(client, story, [group("g", [message("m")])]).status_code == 200
    original = client.get(url(story)).json()

    def fail(*_args):
        raise app_error(503, "TEST_FAILURE", "가상 실패")

    monkeypatch.setattr(repository, "remember_request", fail)
    assert put(client, story, [], "rollback").status_code == 503
    assert client.get(url(story)).json() == original


def test_character_rename_delete_affects_only_linked_messages(context):
    client, _, story, _, dsn = context
    cid = character(dsn, story["id"])
    assert (
        put(
            client,
            story,
            [
                group(
                    "g",
                    [
                        message("mc", "하린 원문", "character", cid),
                        message("mn", "하린 언급", "narrator"),
                        message("mu", "하린 언급"),
                    ],
                )
            ],
        ).status_code
        == 200
    )
    with psycopg.connect(dsn) as connection:
        connection.execute("UPDATE characters SET name='새 이름' WHERE id=%s", (int(cid),))
    renamed = client.get(url(story)).json()
    assert renamed["characters"][0]["name"] == "새 이름"
    assert renamed["fewshots"][0]["messages"][0]["body"] == "하린 원문"
    with psycopg.connect(dsn) as connection:
        connection.execute("DELETE FROM characters WHERE id=%s", (int(cid),))
    remaining = client.get(url(story)).json()["fewshots"][0]["messages"]
    assert [m["speaker_kind"] for m in remaining] == ["narrator", "user"]


def test_owner_deleted_completed_and_cross_endpoint_key_reuse(context):
    client, _, story, owner, dsn = context
    assert put(client, story, [group("g")]).status_code == 200
    expression_url = f"/api/v1/creation/stories/{story['id']}/expression"
    expression = client.get(expression_url).json()["expression"]
    assert (
        client.put(
            expression_url,
            json={"expression": expression},
            headers={"Idempotency-Key": "fewshots-one"},
        ).status_code
        == 409
    )
    with psycopg.connect(dsn) as connection:
        other = connection.execute(
            "INSERT INTO stories(owner_id) VALUES (%s) RETURNING id", (owner + 1000,)
        ).fetchone()[0]
        connection.execute("UPDATE stories SET save_state='private' WHERE id=%s", (story["id"],))
    assert client.get(f"/api/v1/creation/stories/{other}/fewshots").status_code == 404
    assert put(client, story, [], "private").json()["code"] == "FULL_SAVE_REQUIRED"
    with psycopg.connect(dsn) as connection:
        connection.execute(
            "UPDATE stories SET deleted_at=clock_timestamp() WHERE id=%s", (story["id"],)
        )
    for response in (client.get(url(story)), put(client, story, [], "deleted")):
        assert response.status_code == 404
        assert response.json()["code"] == "STORY_NOT_FOUND"


def test_character_projection_reads_live_metadata_without_body_or_writes(context):
    client, _, story, owner_id, dsn = context
    cid = character(dsn, story["id"])
    endpoint = f"/api/v1/creation/stories/{story['id']}/fewshot-characters"
    response = client.get(endpoint)
    assert response.status_code == 200
    assert set(response.json()) == {"story_id", "characters"}
    assert response.json()["characters"][0]["id"] == cid
    with psycopg.connect(dsn) as connection:
        connection.execute("UPDATE characters SET name='실시간 이름' WHERE id=%s", (int(cid),))
    assert client.get(endpoint).json()["characters"][0]["name"] == "실시간 이름"
    with psycopg.connect(dsn) as connection:
        connection.execute("DELETE FROM characters WHERE id=%s", (int(cid),))
    assert client.get(endpoint).json()["characters"] == []
    with psycopg.connect(dsn) as connection:
        saved_at = connection.execute(
            "SELECT saved_at FROM stories WHERE id=%s", (story["id"],)
        ).fetchone()[0]
        assert saved_at == story["saved_at"]
        foreign_id = connection.execute(
            "INSERT INTO stories(owner_id,title) VALUES (%s,'다른 사용자') RETURNING id",
            (owner_id + 10000,),
        ).fetchone()[0]
    assert (
        client.get(f"/api/v1/creation/stories/{foreign_id}/fewshot-characters").status_code == 404
    )
    with psycopg.connect(dsn) as connection:
        connection.execute(
            "UPDATE stories SET deleted_at=clock_timestamp() WHERE id=%s",
            (story["id"],),
        )
    assert client.get(endpoint).status_code == 404


def test_ids_are_numbers_and_string_ids_are_still_accepted(context):
    client, _, story, _, dsn = context
    cid = character(dsn, story["id"])
    saved = put(client, story, [group("g", [message("m", kind="character", character=cid)])])
    data = saved.json()
    assert data["story_id"] == story["id"]
    assert data["characters"][0]["id"] == cid
    group_id = data["fewshots"][0]["id"]
    saved_message = data["fewshots"][0]["messages"][0]
    assert all(isinstance(v, int) for v in (group_id, saved_message["id"], cid))
    assert saved_message["character_id"] == cid
    # 이전 앱이 보내던 문자열 id도 같은 숫자로 받아들인다.
    legacy = [
        group(
            "g",
            [message("m", "수정", "character", str(cid), str(saved_message["id"]))],
            str(group_id),
        )
    ]
    again = put(client, story, legacy, "legacy-string-ids")
    assert again.status_code == 200
    assert again.json()["fewshots"][0]["messages"][0]["id"] == saved_message["id"]


def test_boolean_ids_are_rejected_and_saved_state_is_kept(context):
    client, _, story, _, dsn = context
    cid = character(dsn, story["id"])
    saved = put(client, story, [group("g", [message("m", kind="character", character=cid)])])
    assert saved.status_code == 200
    original = client.get(url(story)).json()
    group_id = original["fewshots"][0]["id"]
    message_id = original["fewshots"][0]["messages"][0]["id"]
    # 저장된 id가 1이어도 JSON 불리언 true를 id 1로 바꾸지 않는다.
    assert (group_id, message_id, cid) == (1, 1, 1)
    cases = [
        [group("g", [message("m", identity=message_id)], True)],
        [group("g", [message("m", identity=True)], group_id)],
        [group("g", [message("m", kind="character", character=True)], group_id)],
    ]
    for index, groups in enumerate(cases):
        response = put(client, story, groups, f"boolean-{index}")
        assert response.status_code == 422, response.text
        assert response.headers["content-type"].startswith("application/problem+json")
        assert response.json()["code"] == "VALIDATION_ERROR"
    assert client.get(url(story)).json() == original
