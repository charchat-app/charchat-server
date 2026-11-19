"""CREATE-US-04 인트로별 장면·공통 관계/호감도 → 7번 공통 저장. 격리 PostgreSQL만 사용."""

from copy import deepcopy

import psycopg
import pytest

from app.features.creation.speech_style.schemas import DEFAULT_EXPRESSION


def url(story):
    return f"/api/v1/creation/stories/{story['id']}/creation"


def put(client, story, body, key):
    return client.put(url(story), json=body, headers={"Idempotency-Key": key})


def intro(key, title="장면", scene=None, body="*비*"):
    return {
        "id": None,
        "client_key": key,
        "title": title,
        "messages": [
            {
                "id": None,
                "client_key": f"{key}-m",
                "speaker_kind": "narrator",
                "character_id": None,
                "body": body,
            }
        ],
        "scene": scene or {},
    }


def relation(key, source, target, label=None):
    def person(value):
        return ("user", None) if value is None else ("character", value)

    (sk, sid), (tk, tid) = person(source), person(target)
    return {
        "client_key": key,
        "source_kind": sk,
        "source_character_id": sid,
        "target_kind": tk,
        "target_character_id": tid,
        "label": label,
    }


def aff(source, target, value):
    """출발 캐릭터 → 상대(None이면 유저) 방향별 호감도."""
    return {
        "source_character_id": source,
        "target_kind": "user" if target is None else "character",
        "target_character_id": target,
        "value": value,
    }


def payload(intros, relations=(), state="draft", affinities=()):
    return {
        "expression": DEFAULT_EXPRESSION.model_dump(),
        "fewshots": [],
        "intros": intros,
        "relations": list(relations),
        "affinities": list(affinities),
        "save_state": state,
        "mode": "manual",
    }


def people(dsn, story, *names):
    ids = []
    with psycopg.connect(dsn) as connection:
        connection.execute("UPDATE stories SET subtitle='한 줄 소개' WHERE id=%s", (story["id"],))
        for position, name in enumerate(names):
            ids.append(
                connection.execute(
                    "INSERT INTO characters(story_id,name,description,position) "
                    "VALUES (%s,%s,'상세',%s) RETURNING id",
                    (story["id"], name, position),
                ).fetchone()[0]
            )
    return ids


def test_draft_restores_scenes_relations_and_raw_partial_values(context):
    client, _, story, _, dsn = context
    a, b = people(dsn, story, "하린", "도윤")
    scene_a = {
        "date_mode": "world",
        "era": "아주 긴 제국력 연호입니다",  # 10자 초과
        "year_input": "302",
        "month_input": "",
        "day_input": None,
        "time_format": "12h",
        "hour_input": "7",
        "minute_input": None,
        "meridiem": "pm",
        "place": "비 오는 골목 " * 5,
        "characters": [
            {"character_id": a, "clothing": "검은 코트\n", "inner_thought": "...?"},
            {"character_id": b, "clothing": "", "inner_thought": None},  # 미지정 → 행 없음
        ],
    }
    scene_b = {"date_mode": "elapsed", "reference_event": "감금", "elapsed_day_input": "3"}
    relations = [
        relation("r1", a, None, "소꿉친구"),
        relation("r2", None, a, "짝사랑"),
        relation("r4", b, a, "라이벌관계입니다요"),  # 초과 원문 보존
    ]
    # AC09: 호감도는 관계 카드와 독립. 관계가 없는 a→b에도 값을 둘 수 있다.
    affinities = [
        aff(a, None, "35"),
        aff(a, b, ""),  # 지운 호감도 보존
        aff(b, a, "150"),  # 초과 원문 보존
    ]
    saved = put(
        client,
        story,
        payload(
            [intro("i1", scene=scene_a), intro("i2", scene=scene_b)],
            relations,
            affinities=affinities,
        ),
        "draft",
    )
    assert saved.status_code == 200, saved.text
    data = client.get(url(story)).json()
    got_a, got_b = data["intros"][0]["scene"], data["intros"][1]["scene"]
    assert got_a["era"] == scene_a["era"] and got_a["year_input"] == "302"
    assert got_a["month_input"] is None and got_a["day_input"] is None
    assert (got_a["time_format"], got_a["hour_input"], got_a["meridiem"]) == ("12h", "7", "pm")
    assert got_a["place"] == scene_a["place"]
    assert got_a["characters"] == [
        {"character_id": a, "clothing": "검은 코트\n", "inner_thought": "...?"}
    ]
    assert got_b["date_mode"] == "elapsed" and got_b["elapsed_day_input"] == "3"
    assert got_b["time_format"] == "24h" and got_b["hour_input"] is None
    assert [r["label"] for r in data["relations"]] == ["소꿉친구", "짝사랑", "라이벌관계입니다요"]
    assert [r["source_kind"] for r in data["relations"]] == ["character", "user", "character"]
    assert data["affinities"] == affinities
    assert saved.json()["affinities"] == affinities
    # 저장 응답과 재조회가 같은 내용이다.
    assert [i["scene"] for i in saved.json()["intros"]] == [i["scene"] for i in data["intros"]]

    # AC02: A만 수정해도 B는 그대로.
    edited = deepcopy(data)
    edited["intros"][0]["scene"]["place"] = "옥상"
    put(
        client,
        story,
        payload(edited["intros"], edited["relations"], affinities=edited["affinities"]),
        "edit-a",
    )
    after = client.get(url(story)).json()
    assert after["intros"][0]["scene"]["place"] == "옥상"
    assert after["intros"][1]["scene"] == got_b
    assert after["relations"] == [
        {**r, "client_key": after["relations"][i]["client_key"]}
        for i, r in enumerate(data["relations"])
    ]
    assert after["affinities"] == affinities


def test_affinity_defaults_to_zero_without_rows(context):
    """AC09: 입력하지 않은 조합은 행을 만들지 않는다(앱이 0으로 표시).

    관계를 지워도 호감도는 남는다."""
    client, _, story, _, dsn = context
    a, b = people(dsn, story, "하린", "도윤")
    put(
        client,
        story,
        payload([intro("i")], [relation("r", a, b, "친구")], affinities=[aff(a, b, "40")]),
        "1",
    )
    put(client, story, payload([intro("i")], [], affinities=[aff(a, b, "40")]), "2")
    data = client.get(url(story)).json()
    assert data["relations"] == [] and data["affinities"] == [aff(a, b, "40")]
    put(client, story, payload([intro("i")]), "3")
    assert client.get(url(story)).json()["affinities"] == []
    with psycopg.connect(dsn) as connection:
        rows = connection.execute(
            "SELECT count(*) FROM character_initial_affinities "
            "WHERE source_character_id IN (%s,%s)",
            (int(a), int(b)),
        ).fetchone()[0]
    assert rows == 0


def test_empty_scene_stays_unspecified(context):
    client, _, story, _, _ = context
    put(client, story, payload([intro("i1")]), "empty")
    scene = client.get(url(story)).json()["intros"][0]["scene"]
    assert scene["date_mode"] is None and scene["time_format"] == "24h"
    assert all(scene[k] is None for k in ("year_input", "hour_input", "minute_input", "place"))
    assert scene["characters"] == []


@pytest.mark.parametrize(
    ("scene", "relations"),
    [
        ({"date_mode": "calendar", "era": "제국력"}, []),  # 다른 방식 입력
        ({"date_mode": None, "year_input": "1"}, []),
        ({"time_format": "24h", "meridiem": "am"}, []),
        ({}, [relation("r", "A", "A")]),  # 같은 인물
        ({}, [relation("r", None, None)]),  # 유저→유저
        ({}, [relation("r", "A", None), relation("s", "A", None)]),  # 같은 방향 중복
    ],
)
def test_rejects_malformed_scene_or_relation(context, scene, relations):
    client, _, story, _, dsn = context
    (a,) = people(dsn, story, "하린")
    fixed = [
        {
            **r,
            "source_character_id": a if r["source_character_id"] else None,
            "target_character_id": a if r["target_character_id"] else None,
        }
        for r in relations
    ]
    result = put(client, story, payload([intro("i", scene=scene)], fixed), "bad")
    assert result.status_code == 422
    assert result.json()["code"] == "VALIDATION_ERROR"


def test_rejects_other_story_people(context):
    client, _, story, _, dsn = context
    people(dsn, story, "하린")
    with psycopg.connect(dsn) as connection:
        other = connection.execute(
            "INSERT INTO stories(owner_id) VALUES (999997) RETURNING id"
        ).fetchone()[0]
        foreign = connection.execute(
            "INSERT INTO characters(story_id,name,description,position) "
            "VALUES (%s,'남','상세',0) RETURNING id",
            (other,),
        ).fetchone()[0]
    scene = {"characters": [{"character_id": foreign, "clothing": "x"}]}
    assert (
        put(client, story, payload([intro("i", scene=scene)]), "s").json()["code"]
        == "INVALID_SCENE_CHARACTER"
    )
    rel = [relation("r", foreign, None)]
    assert (
        put(client, story, payload([intro("i")], rel), "r").json()["code"]
        == "INVALID_RELATION_PERSON"
    )
    assert (
        put(client, story, payload([intro("i")], affinities=[aff(foreign, None, "0")]), "a").json()[
            "code"
        ]
        == "INVALID_AFFINITY_PERSON"
    )


@pytest.mark.parametrize(
    "affinities",
    [
        [aff("A", "A", "0")],  # 자기 자신
        [aff("A", None, "0"), aff("A", None, "1")],  # 같은 방향 중복
        [{**aff("A", None, "0"), "target_kind": "character"}],  # 대상 연결 불일치
        [{**aff("A", None, "0"), "source_character_id": None}],  # 유저 출발(미정)
        [{**aff("A", None, "0"), "value": None}],
    ],
)
def test_rejects_malformed_affinity(context, affinities):
    client, _, story, _, dsn = context
    (a,) = people(dsn, story, "하린")
    fixed = [
        {
            **x,
            "source_character_id": a if x["source_character_id"] else None,
            "target_character_id": a if x["target_character_id"] else None,
        }
        for x in affinities
    ]
    result = put(client, story, payload([intro("i")], affinities=fixed), "bad")
    assert result.status_code == 422
    assert result.json()["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("state", ["private", "public"])
@pytest.mark.parametrize(
    ("scene", "rel", "code"),
    [
        (
            {"date_mode": "calendar", "year_input": "2026", "month_input": "2"},
            None,
            "INITIAL_DATE_INVALID",
        ),
        (
            {"date_mode": "calendar", "year_input": "2024", "month_input": "2", "day_input": "29"},
            None,
            "INITIAL_DATE_INVALID",
        ),
        (
            {"date_mode": "calendar", "year_input": "2026", "month_input": "4", "day_input": "31"},
            None,
            "INITIAL_DATE_INVALID",
        ),
        (
            {"date_mode": "calendar", "year_input": "0", "month_input": "1", "day_input": "1"},
            None,
            "INITIAL_DATE_INVALID",
        ),
        (
            {"date_mode": "world", "year_input": "302", "month_input": "6", "day_input": "1"},
            None,
            "INITIAL_DATE_INVALID",
        ),
        (
            {"date_mode": "elapsed", "reference_event": "감금", "elapsed_day_input": "0"},
            None,
            "INITIAL_DATE_INVALID",
        ),
        (
            {"date_mode": "elapsed", "reference_event": "감금", "elapsed_day_input": "1.5"},
            None,
            "INITIAL_DATE_INVALID",
        ),
        ({"hour_input": "24", "minute_input": "0"}, None, "INITIAL_TIME_INVALID"),
        ({"hour_input": "9"}, None, "INITIAL_TIME_INVALID"),
        (
            {"time_format": "12h", "hour_input": "9", "minute_input": "30"},
            None,
            "INITIAL_TIME_INVALID",
        ),
        (
            {"time_format": "12h", "hour_input": "0", "minute_input": "30", "meridiem": "am"},
            None,
            "INITIAL_TIME_INVALID",
        ),
        ({"place": "가" * 31}, None, "INITIAL_TEXT_TOO_LONG"),
        (
            {"characters": [{"character_id": "A", "clothing": "😀" * 301}]},
            None,
            "INITIAL_TEXT_TOO_LONG",
        ),
        (
            {"characters": [{"character_id": "A", "inner_thought": "가" * 101}]},
            None,
            "INITIAL_TEXT_TOO_LONG",
        ),
        (
            {"date_mode": "elapsed", "reference_event": "가" * 11, "elapsed_day_input": "3"},
            None,
            "INITIAL_TEXT_TOO_LONG",
        ),
        ({}, ("가" * 11, None), "INITIAL_TEXT_TOO_LONG"),
        ({}, (None, ""), "AFFINITY_INVALID"),
        ({}, (None, "101"), "AFFINITY_INVALID"),
        ({}, (None, "3.5"), "AFFINITY_INVALID"),
    ],
)
def test_completed_save_rejects_invalid_initial_state(context, state, scene, rel, code):
    client, _, story, _, dsn = context
    (a,) = people(dsn, story, "하린")
    scene = deepcopy(scene)
    for c in scene.get("characters", []):
        c["character_id"] = a
    label, value = rel or (None, None)
    relations = [relation("r", a, None, label)] if label is not None else []
    affinities = [aff(a, None, value)] if value is not None else []
    before = client.get(url(story)).json()
    result = put(
        client,
        story,
        payload([intro("i", scene=scene)], relations, state, affinities),
        "invalid",
    )
    assert result.status_code == 422, result.text
    assert result.json()["code"] == code
    assert client.get(url(story)).json() == before


def test_completed_save_accepts_boundaries_and_unspecified(context):
    client, _, story, _, dsn = context
    a, b = people(dsn, story, "하린", "도윤")
    scenes = [
        {},  # 날짜·시간 모두 미지정
        {
            "date_mode": "calendar",
            "year_input": "9999",
            "month_input": "02",
            "day_input": "28",
            "hour_input": "23",
            "minute_input": "59",
            "place": "가" * 30,
            "characters": [
                {"character_id": a, "clothing": "😀" * 300, "inner_thought": "가" * 100}
            ],
        },
        {
            "date_mode": "world",
            "era": "가" * 10,
            "year_input": "1",
            "month_input": "12",
            "day_input": "31",
            "time_format": "12h",
            "hour_input": "12",
            "minute_input": "00",
            "meridiem": "pm",
        },
        {"date_mode": "elapsed", "reference_event": "감금", "elapsed_day_input": "9999"},
        {"date_mode": "calendar", "time_format": "12h"},  # 방식만 고르고 입력 없음 = 미지정
    ]
    relations = [
        relation("r1", a, None, "가" * 10),
        relation("r2", b, a, None),
        relation("r3", None, b, "친구"),
    ]
    affinities = [aff(a, None, "-100"), aff(b, a, "100")]
    result = put(
        client,
        story,
        payload(
            [intro(f"i{n}", scene=s) for n, s in enumerate(scenes)],
            relations,
            "private",
            affinities,
        ),
        "ok",
    )
    assert result.status_code == 200, result.text
    assert result.json()["save_state"] == "private"


def test_character_delete_cascades_only_their_state(context):
    client, _, story, _, dsn = context
    a, b = people(dsn, story, "하린", "도윤")
    scene = {
        "place": "옥상",
        "hour_input": "9",
        "minute_input": "0",
        "characters": [
            {"character_id": a, "clothing": "코트"},
            {"character_id": b, "clothing": "교복"},
        ],
    }
    relations = [
        relation("r1", a, None, "친구"),
        relation("r2", b, a, "라이벌"),
        relation("r3", None, a, "짝사랑"),
        relation("r4", b, None, "동료"),
    ]
    affinities = [aff(a, None, "10"), aff(b, a, "-5"), aff(b, None, "20")]
    put(client, story, payload([intro("i", scene=scene)], relations, affinities=affinities), "base")
    with psycopg.connect(dsn) as connection:
        connection.execute("DELETE FROM characters WHERE id=%s", (int(a),))
    data = client.get(url(story)).json()
    got = data["intros"][0]["scene"]
    assert got["characters"] == [{"character_id": b, "clothing": "교복", "inner_thought": None}]
    assert (got["place"], got["hour_input"]) == ("옥상", "9")
    assert [r["label"] for r in data["relations"]] == ["동료"]
    assert data["affinities"] == [aff(b, None, "20")]
    with psycopg.connect(dsn) as connection:
        left = connection.execute(
            "SELECT count(*) FROM character_initial_affinities WHERE source_character_id=%s",
            (int(b),),
        ).fetchone()[0]
    assert left == 1
