"""CHAT-US-08 응답 맨 끝 ```info 블록 떼기·읽기·잇기 순수 규칙(KAN-22, messaging/info.py 계약)."""

import pytest

from app.features.conversation.messaging.info import (
    apply_info,
    format_info_lines,
    parse_info,
    split_info,
)
from app.features.conversation.messaging.parser import parse_assistant_text

NAMES = ["도경", "하린"]
BODY = "@도경: *문을 연다.*\n\n왔어?\n@나레이터: *빗소리.*\n"
INFO = (
    "날짜: 2026년 11월 27일\n시간: 19:30\n장소: 헌책방 온새미\n"
    "[도경] 착장: 젖은 트렌치코트 | 속마음: 사건 생각뿐이다"
)


def empty(**overrides):
    return {"date": None, "time": None, "place": None, "characters": [], **overrides}


# ---- 떼기(split_info) ----


def test_chat_us_08_split_info_takes_closed_block_at_end():
    body, info = split_info(f"{BODY}```info\n{INFO}\n```\n")
    assert body == BODY
    assert info == INFO


def test_chat_us_08_split_info_body_keeps_original_text_and_info_is_not_in_bubbles():
    body, _ = split_info(f"{BODY}\n```info\n{INFO}\n```")
    assert body == BODY + "\n"
    parts = parse_assistant_text(body, NAMES)
    assert [p.body for p in parts] == ["*문을 연다.*\n\n왔어?", "*빗소리.*"]
    assert all("info" not in p.body and "장소" not in p.body for p in parts)


def test_chat_us_08_split_info_without_closing_fence_takes_rest_as_info():
    # 응답이 블록 중간에 끝났다(출력 한도 등). 여는 줄부터 끝까지가 info다.
    body, info = split_info(f"{BODY}```info\n날짜: 2026년 11월 27일\n시간: 19")
    assert body == BODY
    assert info == "날짜: 2026년 11월 27일\n시간: 19"
    # 여는 줄만 막 도착한 스트리밍 중간에도 숨길 본문이 정해진다.
    assert split_info(f"{BODY}```info") == (BODY, "")


def test_chat_us_08_split_info_text_after_block_means_no_info():
    text = f"{BODY}```info\n{INFO}\n```\n@도경: 그리고 한마디 더."
    assert split_info(text) == (text, None)


def test_chat_us_08_split_info_whitespace_after_block_is_allowed():
    body, info = split_info(f"{BODY}```info\n장소: 옥상\n```  \n\n \t\n")
    assert (body, info) == (BODY, "장소: 옥상")


def test_chat_us_08_split_info_handles_crlf_and_cr_line_breaks():
    crlf = BODY.replace("\n", "\r\n") + "```info\r\n장소: 옥상\r\n시간: 21시\r\n```\r\n"
    body, info = split_info(crlf)
    assert body == BODY.replace("\n", "\r\n")
    assert info == "장소: 옥상\n시간: 21시"
    cr = "@도경: 응\r```info\r장소: 옥상\r```"
    assert split_info(cr) == ("@도경: 응\r", "장소: 옥상")


def test_chat_us_08_split_info_uses_last_block_and_trims_fence_lines():
    text = f"```info\n장소: 앞\n```\n{BODY}  ```info  \n장소: 뒤\n  ```  "
    body, info = split_info(text)
    assert info == "장소: 뒤"
    assert body.startswith("```info\n장소: 앞\n```\n")  # 앞 블록은 본문에 남는다


def test_chat_us_08_split_info_empty_block_and_no_block():
    assert split_info(f"{BODY}```info\n```") == (BODY, "")
    assert split_info(BODY) == (BODY, None)
    assert split_info("") == ("", None)
    # 여는 줄은 정확히 ```info 여야 한다(같은 줄 뒤 글·대문자·다른 언어 표시는 아니다).
    for text in (f"{BODY}```info 장소: 옥상\n```", f"{BODY}```INFO\n장소\n```", "```json\n{}\n```"):
        assert split_info(text) == (text, None)


# ---- 읽기(parse_info) ----


def test_chat_us_08_parse_info_reads_every_field():
    parsed = parse_info(
        INFO + "\n[하린] 착장: 노란 카디건 | 속마음: 손님이 반갑다", "calendar", NAMES
    )
    assert parsed == {
        "date": {
            "mode": "calendar",
            "era": None,
            "year": 2026,
            "month": 11,
            "day": 27,
            "reference_event": None,
            "elapsed_day": None,
        },
        "time": {"hour": 19, "minute": 30},
        "place": "헌책방 온새미",
        "characters": [
            {"name": "도경", "clothing": "젖은 트렌치코트", "inner_thought": "사건 생각뿐이다"},
            {"name": "하린", "clothing": "노란 카디건", "inner_thought": "손님이 반갑다"},
        ],
    }


@pytest.mark.parametrize(
    "mode, line, expected",
    [
        ("calendar", "2026년 2월 28일", {"year": 2026, "month": 2, "day": 28}),
        ("calendar", "2026년2월 3일", {"year": 2026, "month": 2, "day": 3}),
        ("world", "대한 1898년 3월 2일", {"era": "대한", "year": 1898, "month": 3, "day": 2}),
        (
            "world",
            "제국력 원년 12년 1월 1일",
            {"era": "제국력 원년", "year": 12, "month": 1, "day": 1},
        ),
        ("elapsed", "감금 3일째", {"reference_event": "감금", "elapsed_day": 3}),
        ("elapsed", "3일째", {"reference_event": None, "elapsed_day": 3}),
    ],
)
def test_chat_us_08_parse_info_date_by_mode(mode, line, expected):
    date = parse_info(f"날짜: {line}", mode, NAMES)["date"]
    assert date["mode"] == mode
    assert {key: date[key] for key in expected} == expected


@pytest.mark.parametrize(
    "mode, line",
    [
        ("calendar", "2026년 2월 29일"),  # 윤년 없음(생성 4번과 같은 달력)
        ("calendar", "2026년 4월 31일"),
        ("calendar", "2026년 13월 1일"),
        ("calendar", "0년 1월 1일"),
        ("calendar", "10000년 1월 1일"),
        ("calendar", "11월 27일"),  # 연도 없음
        ("calendar", "대한 1898년 3월 2일"),  # 다른 방식 모양
        ("calendar", "감금 3일째"),
        ("calendar", "２０２６년 １월 １일"),  # 전각 숫자
        ("world", "1898년 3월 2일"),  # 연호 없음
        ("world", "열한글자가넘는연호입니다 1898년 3월 2일"),  # 연호 10자 초과
        ("world", "대한 1898년 2월 29일"),
        ("elapsed", "감금 0일째"),
        ("elapsed", "감금 이후 사흘째"),
        ("elapsed", "열한글자기준사건입니다 3일째"),  # 기준 사건 10자 초과
        ("elapsed", "2026년 11월 27일"),
        (None, "2026년 11월 27일"),  # 날짜 방식이 없는 세션
        ("calendar", "-"),
        ("calendar", ""),
    ],
)
def test_chat_us_08_parse_info_drops_unreadable_date(mode, line):
    assert parse_info(f"날짜: {line}", mode, NAMES)["date"] is None


def test_chat_us_08_parse_info_time_forms():
    assert parse_info("시간: 07:05", None, NAMES)["time"] == {"hour": 7, "minute": 5}
    assert parse_info("시간: 0:00", None, NAMES)["time"] == {"hour": 0, "minute": 0}
    assert parse_info("시간: 19시", None, NAMES)["time"] == {"hour": 19, "minute": None}
    for bad in ("24:00", "19:60", "19:5", "오후 7시", "19시 30분", "7 PM", "-", "시간"):
        assert parse_info(f"시간: {bad}", None, NAMES)["time"] is None, bad


def test_chat_us_08_parse_info_drops_values_over_limits():
    over = parse_info(
        "\n".join(
            [
                "장소: " + "가" * 31,
                "[도경] 착장: " + "가" * 301 + " | 속마음: " + "가" * 100,
                "[하린] 착장: " + "가" * 300 + " | 속마음: " + "가" * 101,
            ]
        ),
        "calendar",
        NAMES,
    )
    assert over["place"] is None
    assert over["characters"] == [
        {"name": "도경", "clothing": None, "inner_thought": "가" * 100},
        {"name": "하린", "clothing": "가" * 300, "inner_thought": None},
    ]
    assert parse_info("장소: " + "가" * 30, None, NAMES)["place"] == "가" * 30


def test_chat_us_08_parse_info_ignores_unknown_and_user_character_lines():
    parsed = parse_info(
        "[민서] 착장: 교복 | 속마음: 떨린다\n[모르는 사람] 착장: 코트\n[도경] 착장: 셔츠",
        None,
        NAMES,
    )
    assert parsed["characters"] == [{"name": "도경", "clothing": "셔츠", "inner_thought": None}]


def test_chat_us_08_parse_info_dash_or_missing_items_are_not_values():
    parsed = parse_info(
        "장소: -\n[도경] 착장: - | 속마음: -\n[하린] 속마음: 졸리다\n아무 말\n날짜 2026년",
        "calendar",
        NAMES,
    )
    assert parsed == empty(
        characters=[{"name": "하린", "clothing": None, "inner_thought": "졸리다"}]
    )


def test_chat_us_08_parse_info_empty_info():
    assert parse_info("", "calendar", NAMES) == empty()
    assert parse_info("\n  \n", "calendar", NAMES) == empty()


def test_chat_us_08_parse_info_later_valid_line_wins_invalid_is_skipped():
    parsed = parse_info("장소: 앞\n장소: 뒤\n시간: 10:00\n시간: 25:00", None, NAMES)
    assert (parsed["place"], parsed["time"]) == ("뒤", {"hour": 10, "minute": 0})


# ---- 잇기(apply_info)와 [현재 장면] 줄 형식 ----

SCENE = {
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
STATES = [
    {"name": "도경", "clothing": "젖은 코트", "inner_thought": "피곤하다"},
    {"name": "하린", "clothing": None, "inner_thought": None},
]


def test_chat_us_08_apply_info_overwrites_only_parsed_items():
    parsed = parse_info("시간: 12시\n장소: 공원\n[하린] 속마음: 신난다", "calendar", NAMES)
    scene, states = apply_info(SCENE, STATES, parsed)
    assert scene == {**SCENE, "hour": 12, "minute": None, "place": "공원"}
    assert states == [STATES[0], {"name": "하린", "clothing": None, "inner_thought": "신난다"}]
    # 입력은 바꾸지 않는다.
    assert SCENE["place"] == "경찰서 옥상" and STATES[1]["inner_thought"] is None


def test_chat_us_08_apply_info_elapsed_without_event_keeps_event():
    scene = {"date_mode": "elapsed", "reference_event": "감금", "elapsed_day": 3}
    after, _ = apply_info(scene, [], parse_info("날짜: 4일째", "elapsed", []))
    assert (after["reference_event"], after["elapsed_day"]) == ("감금", 4)


def test_chat_us_08_format_info_lines_round_trips_through_parse():
    lines = format_info_lines(SCENE, STATES)
    assert lines == [
        "날짜: 2026년 11월 22일",
        "시간: 21:05",
        "장소: 경찰서 옥상",
        "[도경] 착장: 젖은 코트 | 속마음: 피곤하다",
        "[하린] 착장: - | 속마음: -",
    ]
    scene, states = apply_info(
        {"date_mode": "calendar"}, STATES, parse_info("\n".join(lines), "calendar", NAMES)
    )
    assert {k: scene.get(k) for k in SCENE} == SCENE
    assert states == STATES


def test_chat_us_08_format_info_lines_has_no_invented_defaults():
    assert format_info_lines({"hour": 21, "minute": None}, []) == ["시간: 21시"]
    assert format_info_lines({"date_mode": "elapsed", "elapsed_day": 3}, []) == ["날짜: 3일째"]
    assert format_info_lines(
        {"date_mode": "elapsed", "reference_event": "감금", "elapsed_day": 3}, []
    ) == ["날짜: 감금 3일째"]
    assert format_info_lines(
        {"date_mode": "world", "era": "대한", "year": 1898, "month": 3, "day": 2}, []
    ) == ["날짜: 대한 1898년 3월 2일"]
    assert format_info_lines(None, []) == []
