"""CHAT-US-08 장면 변화 규칙(순수 함수): 판단 결과 검증과 장면 적용."""

import pytest

from app.features.conversation.messaging.prompt import PromptContext, PromptPart
from app.features.conversation.scene.judge import build_judge_messages, describe_scene
from app.features.conversation.scene.rules import (
    CharacterPatch,
    ScenePatch,
    advance_date,
    apply_patch,
    evidence_found,
    parse_judgement,
)

NAMES = ["도경", "Bea"]
CHARS = [
    {"id": 1, "name": "도경", "clothing": "젖은 코트", "inner_thought": "피곤하다"},
    {"id": 2, "name": "Bea", "clothing": None, "inner_thought": None},
]


def calendar_scene(**overrides):
    scene = {
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
    }
    scene.update(overrides)
    return scene


def test_chat_us_08_ac01_no_change_or_unreadable_judgement_keeps_scene():
    assert parse_judgement('{"changed": false}', NAMES) is None
    assert parse_judgement("장면이 바뀌지 않았다", NAMES) is None
    assert parse_judgement('{"changed": "yes", "place": "공원"}', NAMES) is None
    # 바뀌었다고 했지만 쓸 수 있는 항목이 없으면 변화 없음이다.
    assert parse_judgement('{"changed": true, "place": "  ", "evidence": "x"}', NAMES) is None
    # 근거 문장이 없으면 바꾸지 않는다.
    assert parse_judgement('{"changed": true, "place": "공원"}', NAMES) is None
    assert parse_judgement('{"changed": true, "place": "공원", "evidence": " "}', NAMES) is None


def test_chat_us_08_ac02_parse_reads_fenced_json_and_validates_fields():
    patch = parse_judgement(
        """```json
{"changed": true, "place": "공원 벤치", "days_passed": 1,
 "time": {"hour": 12, "minute": null},
 "characters": [{"name": "도경", "clothing": "회색 셔츠"},
                {"name": "민서", "clothing": "원피스"},
                {"name": "Bea", "clothing": null, "inner_thought": null}],
 "evidence": "다음 날 점심, 공원 벤치", "evidence_from": "user"}
```""",
        NAMES,
    )
    assert patch == ScenePatch(
        place="공원 벤치",
        days_passed=1,
        hour=12,
        minute=None,
        characters=(CharacterPatch("도경", clothing="회색 셔츠"),),
        evidence="다음 날 점심, 공원 벤치",
        evidence_from="user",
    )


@pytest.mark.parametrize(
    "payload",
    [
        '{"changed": true, "date": {"month": 2, "day": 30}}',  # 2월은 28일까지
        '{"changed": true, "date": {"month": 13, "day": 1}}',
        '{"changed": true, "time": {"hour": 24, "minute": 0}}',
        '{"changed": true, "time": {"minute": 30}}',  # 시 없이 분만
        '{"changed": true, "days_passed": 0}',
        '{"changed": true, "days_passed": true}',
        '{"changed": true, "place": "a\\u0000b"}',  # PostgreSQL text에 넣을 수 없는 NUL
        '{"changed": true, "place": "' + "가" * 31 + '"}',  # 생성 4번 장소 상한 30자
        '{"changed": true, "new_reference_event": "' + "가" * 11 + '"}',
        '{"changed": true, "date": {"year": 10000, "month": 1, "day": 1}}',
        '{"changed": true, "characters": [{"name": ["도경"], "clothing": "코트"}]}',
        '{"changed": true, "characters": [{"name": {"a": 1}, "clothing": "코트"}]}',
        '{"changed": true, "characters": [{"name": "없는 사람", "clothing": "코트"}]}',
    ],
)
def test_chat_us_08_invalid_fields_are_dropped(payload):
    payload = payload[:-1] + ', "evidence": "근거"}'
    assert parse_judgement(payload, NAMES) is None


def test_chat_us_08_evidence_from_defaults_to_assistant():
    patch = parse_judgement('{"changed": true, "place": "카페", "evidence": "카페로 갔다"}', NAMES)
    assert patch is not None
    assert patch.evidence_from == "assistant"


@pytest.mark.parametrize(
    "start, days, end",
    [
        ((2026, 11, 22), 1, (2026, 11, 23)),
        ((2026, 2, 28), 1, (2026, 3, 1)),  # 윤년 없음(생성 4번 달력)
        ((2026, 12, 31), 1, (2027, 1, 1)),
        ((None, 12, 30), 3, (None, 1, 2)),  # 연도를 모르면 연도는 그대로 비운다
        ((2026, 1, 31), 30, (2026, 3, 2)),
    ],
)
def test_chat_us_08_advance_date_uses_creation_calendar(start, days, end):
    assert advance_date(*start, days) == end


def test_chat_us_08_ac02_next_day_park_updates_date_time_place_only():
    patch = ScenePatch(place="공원 벤치", days_passed=1, hour=12, minute=None)
    scene, changed = apply_patch(calendar_scene(), CHARS, patch)
    assert scene == calendar_scene(day=23, hour=12, minute=None, place="공원 벤치")
    assert changed == []


def test_chat_us_08_explicit_date_wins_and_keeps_year_when_missing():
    scene, _ = apply_patch(calendar_scene(), CHARS, ScenePatch(month=12, day=25, days_passed=3))
    assert (scene["year"], scene["month"], scene["day"]) == (2026, 12, 25)


def test_chat_us_08_unspecified_date_is_not_invented():
    scene, _ = apply_patch(
        calendar_scene(year=None, month=None, day=None), CHARS, ScenePatch(days_passed=3)
    )
    assert (scene["year"], scene["month"], scene["day"]) == (None, None, None)


def test_chat_us_08_unspecified_time_is_filled_only_when_stated():
    scene, _ = apply_patch(calendar_scene(hour=None, minute=None), CHARS, ScenePatch(place="카페"))
    assert (scene["hour"], scene["minute"]) == (None, None)
    scene, _ = apply_patch(calendar_scene(hour=None, minute=None), CHARS, ScenePatch(hour=23))
    assert (scene["hour"], scene["minute"]) == (23, None)


def test_chat_us_08_world_date_advances_month_and_day():
    scene, _ = apply_patch(
        calendar_scene(date_mode="world", era="왕국력", year=1898, month=4, day=30),
        CHARS,
        ScenePatch(days_passed=1),
    )
    assert (scene["era"], scene["year"], scene["month"], scene["day"]) == ("왕국력", 1898, 5, 1)


def elapsed_scene(**overrides):
    values = dict(
        date_mode="elapsed", year=None, month=None, day=None, reference_event="감금", elapsed_day=3
    )
    values.update(overrides)
    return calendar_scene(**values)


def test_chat_us_08_elapsed_day_counts_up():
    scene, _ = apply_patch(elapsed_scene(), CHARS, ScenePatch(days_passed=1))
    assert (scene["reference_event"], scene["elapsed_day"]) == ("감금", 4)


def test_chat_us_08_new_reference_event_starts_at_day_one():
    scene, _ = apply_patch(
        elapsed_scene(), CHARS, ScenePatch(new_reference_event="탈출", days_passed=2)
    )
    assert (scene["reference_event"], scene["elapsed_day"]) == ("탈출", 1)


def test_chat_us_08_elapsed_without_day_stays_unspecified():
    scene, _ = apply_patch(elapsed_scene(elapsed_day=None), CHARS, ScenePatch(days_passed=1))
    assert scene["elapsed_day"] is None


def test_chat_us_08_date_mode_is_not_switched_or_created():
    # 날짜 방식이 없는 세션에서는 날짜를 만들지 않는다.
    scene, _ = apply_patch(
        calendar_scene(date_mode=None, year=None, month=None, day=None),
        CHARS,
        ScenePatch(month=5, day=1, new_reference_event="탈출"),
    )
    assert (scene["date_mode"], scene["month"], scene["day"]) == (None, None, None)
    # 달력 방식에서는 기준 사건을 쓰지 않는다.
    scene, _ = apply_patch(calendar_scene(), CHARS, ScenePatch(new_reference_event="탈출"))
    assert scene["reference_event"] is None


def test_chat_us_08_only_named_character_changes():
    scene, changed = apply_patch(
        calendar_scene(),
        CHARS,
        ScenePatch(
            characters=(
                CharacterPatch("도경", clothing="회색 셔츠"),
                CharacterPatch("없는 사람", clothing="코트"),
            )
        ),
    )
    assert scene == calendar_scene()
    assert changed == [
        {"id": 1, "name": "도경", "clothing": "회색 셔츠", "inner_thought": "피곤하다"}
    ]
    assert CHARS[0]["clothing"] == "젖은 코트"  # 입력은 바꾸지 않는다


def test_chat_us_08_same_values_are_not_a_change():
    scene, changed = apply_patch(
        calendar_scene(),
        CHARS,
        ScenePatch(place="경찰서 옥상", characters=(CharacterPatch("도경", clothing="젖은 코트"),)),
    )
    assert scene == calendar_scene()
    assert changed == []


def test_chat_us_08_year_without_value_rolls_forward_for_earlier_date():
    scene, _ = apply_patch(calendar_scene(month=12, day=31), CHARS, ScenePatch(month=1, day=1))
    assert (scene["year"], scene["month"], scene["day"]) == (2027, 1, 1)


def test_chat_us_08_passing_midnight_advances_one_day():
    scene, _ = apply_patch(calendar_scene(hour=23, minute=40), CHARS, ScenePatch(hour=1))
    assert (scene["day"], scene["hour"], scene["minute"]) == (23, 1, None)
    # 판단이 지난 날 수를 함께 보내면 그 값만 쓴다(이틀로 세지 않는다).
    scene, _ = apply_patch(
        calendar_scene(hour=23, minute=40), CHARS, ScenePatch(hour=1, days_passed=1)
    )
    assert scene["day"] == 23


def test_chat_us_08_same_hour_keeps_stored_minute():
    scene, _ = apply_patch(calendar_scene(hour=19, minute=30), CHARS, ScenePatch(hour=19))
    assert (scene["hour"], scene["minute"]) == (19, 30)


def test_chat_us_08_year_never_passes_creation_limit():
    scene, _ = apply_patch(
        calendar_scene(year=9999, month=12, day=31), CHARS, ScenePatch(days_passed=1)
    )
    assert (scene["year"], scene["month"], scene["day"]) == (9999, 12, 31)


@pytest.mark.parametrize(
    "evidence, sources, found",
    [
        ("다음 날 점심, 공원 벤치", ["[나레이터] *다음 날 점심, 공원 벤치.*"], True),
        ("도경이 코트를 벗었다", ["코트 좀 벗어요.", "@도경: *도경이 코트를 벗었다.*"], True),
        ("둘은 바다로 갔다", ["카페에 있었다."], False),
        ("...", ["..."], False),
        ("가자", ["가자."], False),  # 너무 짧은 근거
    ],
)
def test_chat_us_08_evidence_must_appear_in_this_turn(evidence, sources, found):
    assert evidence_found(evidence, *sources) is found


def test_chat_us_08_judge_anchors_scene_to_user_character():
    """장면의 기준은 이용자 캐릭터다(11-27 PM 결정). 판단 요청의 지침에 그대로 들어간다."""
    context = PromptContext(
        title="제목",
        subtitle="",
        world_description="",
        tags=[],
        characters=[],
        expression={},
        fewshots=[],
        intro=[],
        play_name="민서",
        profile_description="",
        scene={"place": "옥상"},
        character_states=[],
        affinities=[],
        relationships=[],
        session_character_names=["도경"],
        current=[PromptPart("user", "민서", "도경은 먼저 내려갔다.")],
    )
    system = build_judge_messages(context, "@도경: 먼저 갈게.")[0]["content"]
    rule = (
        "- 장면의 기준은 이용자 캐릭터다. 장소·시각·날짜는 이용자 캐릭터가 있는 곳과 겪는 "
        "시간을 따른다. 다른 캐릭터만 다른 곳으로 떠나거나 다른 곳에 있다는 서술은 장소 "
        "변화가 아니다. 이용자 캐릭터가 이동했을 때만 장소를 바꾼다. 이용자 캐릭터가 장면에 "
        "나오지 않고 나레이터로만 진행하면 이야기가 이어지는 중심 장면을 따른다."
    )
    assert rule in system
    lines = system.splitlines()
    assert lines[lines.index(rule) - 1].startswith("- 장소: 다른 곳으로 이동한 것이 분명할 때만")


def test_chat_us_08_user_edit_note_in_judge_scene_only_when_edited():
    note = "※ 이용자가 방금 이 장면을 직접 고쳤다. 앞 대화와 달라도 이 장면에서 이어 간다."
    states = [{"name": "도경", "clothing": "회색 셔츠", "inner_thought": None}]
    edited = describe_scene({"place": "공원"}, states, user_edited=True)
    assert edited.endswith("- 도경: 착장 회색 셔츠 / 속마음 미지정\n" + note)
    assert note not in describe_scene({"place": "공원"}, states)
