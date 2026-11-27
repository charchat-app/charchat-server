"""CHAT-US-08 응답 맨 끝 ```info 블록 떼기·읽기·잇기 순수 규칙(KAN-22, messaging/info.py 계약)."""

import pytest

from app.features.conversation.messaging.info import (
    apply_info,
    format_info_lines,
    parse_info,
    scene_view,
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


# ---- 떼기(split_info): 서버·앱 공통 계약 A 규칙 1~6 ----

BODY_LINES = BODY.rstrip("\n")  # 규칙 6: 본문은 줄을 \n으로 다시 이은 것(끝 줄바꿈 없음)


def test_chat_us_08_split_info_takes_closed_block_at_end():
    body, info = split_info(f"{BODY}```info\n{INFO}\n```\n")
    assert body == BODY_LINES
    assert info == INFO


def test_chat_us_08_split_info_body_has_no_info_and_bubbles_have_no_info():
    body, _ = split_info(f"{BODY}\n```info\n{INFO}\n```")
    assert body == BODY  # 블록 앞 빈 줄까지가 본문이다
    parts = parse_assistant_text(body, NAMES)
    assert [p.body for p in parts] == ["*문을 연다.*\n\n왔어?", "*빗소리.*"]
    assert all("info" not in p.body and "장소" not in p.body for p in parts)


def test_chat_us_08_split_info_without_closing_fence_takes_rest_as_info():
    # 규칙 5: 응답이 블록 중간에 끝났다(출력 한도 등). 여는 줄부터 끝까지가 블록이다.
    body, info = split_info(f"{BODY}```info\n날짜: 2026년 11월 27일\n시간: 19")
    assert body == BODY_LINES
    assert info == "날짜: 2026년 11월 27일\n시간: 19"
    assert split_info(f"{BODY}```info") == (BODY_LINES, "")


def test_chat_us_08_split_info_text_after_closing_fence_goes_back_to_body():
    # 규칙 6: 닫는 줄 뒤의 줄은 본문으로 돌아가고, info는 그대로 읽는다.
    text = f"{BODY}```info\n{INFO}\n```\n@도경: 그리고 한마디 더.\n(계속)"
    body, info = split_info(text)
    assert info == INFO
    assert body == f"{BODY_LINES}\n@도경: 그리고 한마디 더.\n(계속)"
    parts = parse_assistant_text(body, NAMES)
    assert [p.body for p in parts][-1] == "그리고 한마디 더.\n(계속)"
    assert all("```" not in p.body and "장소" not in p.body for p in parts)


def test_chat_us_08_split_info_whitespace_after_block_stays_in_body_as_blank_lines():
    body, info = split_info(f"{BODY}```info\n장소: 옥상\n```  \n\n \t\n")
    assert (body, info) == (f"{BODY_LINES}\n\n \t", "장소: 옥상")
    assert [p.body for p in parse_assistant_text(body, NAMES)][-1] == "*빗소리.*"


def test_chat_us_08_split_info_handles_crlf_and_cr_line_breaks():
    crlf = BODY.replace("\n", "\r\n") + "```info\r\n장소: 옥상\r\n시간: 21시\r\n```\r\n"
    body, info = split_info(crlf)
    assert body == BODY_LINES
    assert info == "장소: 옥상\n시간: 21시"
    cr = "@도경: 응\r```info\r장소: 옥상\r```"
    assert split_info(cr) == ("@도경: 응", "장소: 옥상")


@pytest.mark.parametrize(
    "separator",
    ["\n", "\r\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"],
)
def test_chat_us_08_split_info_every_line_separator(separator):
    # 규칙 1: str.splitlines()의 줄 경계 전부(앱 splitPythonLines와 같다).
    text = separator.join(["@도경: 응", "```info", "장소: 옥상", "시간: 21시", "```", "끝"])
    assert split_info(text) == ("@도경: 응\n끝", "장소: 옥상\n시간: 21시")


def test_chat_us_08_split_info_uses_last_block_and_ignores_only_space_and_tab_on_fences():
    text = f"```info\n장소: 앞\n```\n{BODY}  ```info\t\n장소: 뒤\n \t```  "
    body, info = split_info(text)
    assert info == "장소: 뒤"
    # 규칙 5(11-27 개정): 앞 블록도 본문에서 빠지고, info는 마지막 블록이다.
    assert body == BODY_LINES


# ---- 규칙 4~6 개정(11-27): 블록을 모두 빼고 마지막 블록을 info로 ----


def test_chat_us_08_split_info_removes_every_complete_block():
    text = f"{BODY}```info\n장소: 앞\n```\n@도경: 그리고.\n```info\n장소: 뒤\n```"
    body, info = split_info(text)
    assert info == "장소: 뒤"
    assert body == f"{BODY_LINES}\n@도경: 그리고."
    assert "```" not in body and "장소" not in body


def test_chat_us_08_split_info_block_then_text_then_unclosed_block():
    # 마지막 블록이 닫히지 않았다(끝까지가 블록). 앞 블록과 사이 본문은 본문 규칙대로.
    text = f"{BODY}```info\n장소: 앞\n```\n@나레이터: *잠시 뒤.*\n```info\n장소: 뒤\n시간: 19"
    assert split_info(text) == (f"{BODY_LINES}\n@나레이터: *잠시 뒤.*", "장소: 뒤\n시간: 19")


def test_chat_us_08_split_info_opener_inside_block_is_not_an_opener():
    # 블록 안의 ```info 줄은 여는 줄이 아니다. 블록은 처음 닫는 줄에서 끝나고, 그 뒤 글은 본문이다.
    text = f"{BODY}```info\n장소: 앞\n```info\n장소: 안\n```\n@도경: 끝."
    body, info = split_info(text)
    assert info == "장소: 앞\n```info\n장소: 안"
    assert body == f"{BODY_LINES}\n@도경: 끝."
    # 닫는 줄 없이 블록 안에 여는 줄이 또 있어도 끝까지 한 블록이다.
    assert split_info(f"{BODY}```info\n장소: 앞\n```info\n장소: 뒤") == (
        BODY_LINES,
        "장소: 앞\n```info\n장소: 뒤",
    )


def test_chat_us_08_split_info_real_mid_reply_block_does_not_leak_into_bubbles():
    # 실제 모델 65턴 실행에서 나온 모양: 응답 중간 info → '...' → 발화자 블록 → 맨 끝 info.
    text = (
        "@나레이터: *도경이 문을 닫는다.*\n"
        "@도경: 들어와.\n"
        "```info\n"
        "날짜: 2026년 11월 27일\n시간: 19:30\n장소: 헌책방 온새미\n"
        "[도경] 착장: 젖은 트렌치코트 | 속마음: 사건 생각뿐이다\n"
        "```\n"
        "...\n"
        "@하린: 늦었네요.\n"
        "@나레이터: *빗소리가 커진다.*\n"
        "\n"
        "```info\n"
        "날짜: 2026년 11월 27일\n시간: 19:40\n장소: 헌책방 온새미\n"
        "[도경] 착장: 젖은 트렌치코트 | 속마음: 하린이 신경 쓰인다\n"
        "```"
    )
    body, info = split_info(text)
    assert info.splitlines()[1] == "시간: 19:40"
    parts = parse_assistant_text(body, NAMES)
    assert [(p.character_name, p.body) for p in parts] == [
        (None, "*도경이 문을 닫는다.*"),
        ("도경", "들어와.\n..."),
        ("하린", "늦었네요."),
        (None, "*빗소리가 커진다.*"),
    ]
    assert all("```" not in p.body and "날짜" not in p.body for p in parts)


@pytest.mark.parametrize(
    "fence",
    [
        "\ufeff```info",
        "```info\ufeff",
        "\xa0```info",
        "```info\xa0",
        "\u3000```info",
        "```info\u200b",
    ],
)
def test_chat_us_08_split_info_bom_or_nbsp_around_opening_fence_is_not_a_fence(fence):
    # 규칙 2: 공백(U+0020)·탭(U+0009) 말고 다른 문자가 붙으면 여는 줄이 아니다.
    text = f"{BODY}{fence}\n장소: 옥상\n```"
    assert split_info(text) == (text, None)


@pytest.mark.parametrize("closing", ["\ufeff```", "```\xa0", "\xa0```"])
def test_chat_us_08_split_info_bom_or_nbsp_around_closing_fence_is_not_a_fence(closing):
    # 규칙 3: 닫는 줄도 공백·탭만 무시한다. 닫는 줄이 없으면 끝까지가 info다(규칙 5).
    body, info = split_info(f"{BODY}```info\n장소: 옥상\n{closing}\n@도경: 뒤")
    assert body == BODY_LINES
    assert info == f"장소: 옥상\n{closing}\n@도경: 뒤"


def test_chat_us_08_split_info_other_fenced_blocks_stay_in_body():
    before = '```json\n{"a": 1}\n```'
    after = "```python\nprint(1)\n```"
    body, info = split_info(f"{before}\n{BODY}```info\n장소: 옥상\n```\n{after}")
    assert info == "장소: 옥상"
    assert body == f"{before}\n{BODY_LINES}\n{after}"
    # info 없이 다른 펜스만 있으면 원문 그대로가 본문이다.
    text = f"{BODY}{before}\n"
    assert split_info(text) == (text, None)


def test_chat_us_08_split_info_empty_block_and_no_block():
    assert split_info(f"{BODY}```info\n```") == (BODY_LINES, "")
    assert split_info(BODY) == (BODY, None)  # 여는 줄이 없으면 원문 그대로(줄바꿈 포함)
    assert split_info("") == ("", None)
    # 여는 줄은 정확히 ```info 여야 한다(같은 줄 뒤 글·대문자·다른 언어 표시는 아니다).
    for text in (f"{BODY}```info 장소: 옥상\n```", f"{BODY}```INFO\n장소\n```", "```json\n{}\n```"):
        assert split_info(text) == (text, None)
    # 발화자 줄 끝에 붙은 펜스는 여는 줄이 아니다(지침이 새 줄에 쓰게 한다).
    same_line = "@나레이터: ```info\n장소: 옥상\n```"
    assert split_info(same_line) == (same_line, None)


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


@pytest.mark.parametrize(
    "value",
    [
        "<장소>",
        "<착장>",
        "< 아무 글 >",
        "<장소>.",
        "-",
        "?",
        "미정",
        "모름",
        "없음",
        "알 수 없음",
        "지정 없음",
        "미지정",
        " 미정. ",
        "모름...",
        ".",
        "",
    ],
)
def test_chat_us_08_parse_info_placeholder_values_are_not_values(value):
    # 규칙 10: 자리 표시·모름 같은 말은 값이 아니다 → 그 항목은 앞 상태를 유지한다.
    parsed = parse_info(
        f"장소: {value}\n[도경] 착장: {value} | 속마음: {value}\n[하린] 속마음: {value}",
        "calendar",
        NAMES,
    )
    assert parsed == empty()
    scene, states = apply_info(SCENE, STATES, parsed)
    assert (scene, states) == (SCENE, STATES)


def test_chat_us_08_parse_info_values_that_only_contain_placeholder_words_are_values():
    parsed = parse_info(
        "장소: 미정인 골목\n[도경] 착장: <낡은> 코트 | 속마음: 모름지기", None, NAMES
    )
    assert parsed["place"] == "미정인 골목"
    assert parsed["characters"] == [
        {"name": "도경", "clothing": "<낡은> 코트", "inner_thought": "모름지기"}
    ]


@pytest.mark.parametrize(
    "mode, line",
    [
        ("elapsed", "<기준 사건> 3일째"),
        ("elapsed", "미정 3일째"),
        ("world", "<연호> 1898년 3월 2일"),
        ("world", "모름 1898년 3월 2일"),
        ("calendar", "<연>년 <월>월 <일>일"),
        ("elapsed", "<기준 사건> <N>일째"),
    ],
)
def test_chat_us_08_parse_info_placeholder_in_date_drops_the_date_line(mode, line):
    assert parse_info(f"날짜: {line}", mode, NAMES)["date"] is None


@pytest.mark.parametrize(
    "line",
    [
        "2026년\x1f11월 27일",  # Python \s 이지만 JS \s 가 아니다
        "2026년\ufeff11월 27일",  # JS \s 이지만 Python \s 가 아니다
        "2026년\xa011월 27일",
        "2026년\u300011월 27일",
        "2026년\n11월 27일",
    ],
)
def test_chat_us_08_parse_info_date_gaps_are_only_space_or_tab(line):
    # 규칙 11: 날짜 낱말 사이는 반각 공백·탭만(앱과 서버가 같은 값을 읽게 \s를 쓰지 않는다).
    assert parse_info(f"날짜: {line}", "calendar", NAMES)["date"] is None
    assert parse_info("날짜: 2026년\t11월 \t27일", "calendar", NAMES)["date"]["day"] == 27
    assert parse_info(f"날짜: 대한{line[5]}1898년 3월 2일", "world", NAMES)["date"] is None
    assert parse_info(f"날짜: 감금{line[5]}3일째", "elapsed", NAMES)["date"] is None


def test_chat_us_08_parse_info_character_names_may_contain_brackets():
    names = ["A]B", "A", "도경"]
    parsed = parse_info(
        "[A]B] 착장: 흰 셔츠 | 속마음: 떨린다\n[A] 착장: 검은 코트\n[A]C] 착장: 모르는 사람",
        None,
        names,
    )
    assert parsed["characters"] == [
        {"name": "A]B", "clothing": "흰 셔츠", "inner_thought": "떨린다"},
        {"name": "A", "clothing": "검은 코트", "inner_thought": None},
    ]


def test_chat_us_08_parse_info_clothing_may_contain_pipe_and_splits_on_inner_thought_key():
    parsed = parse_info(
        "[도경] 착장: 흰 셔츠 | 청바지 | 운동화 | 속마음: 떨린다 | 그래도 간다\n"
        "[하린] 착장:노란 카디건|속마음:  졸리다",
        None,
        NAMES,
    )
    assert parsed["characters"] == [
        {
            "name": "도경",
            "clothing": "흰 셔츠 | 청바지 | 운동화",
            "inner_thought": "떨린다 | 그래도 간다",
        },
        {"name": "하린", "clothing": "노란 카디건", "inner_thought": "졸리다"},
    ]
    # 이름 뒤가 착장·속마음 키가 아니면 그 줄은 읽지 않는다.
    for line in ("[도경] 흰 셔츠", "[도경] 기분: 좋음", "[도경]", "[도경] 속마음 떨린다"):
        assert parse_info(line, None, NAMES)["characters"] == [], line


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


def test_chat_us_08_format_info_lines_partial_date_is_shown_but_not_read_back():
    # 생성 4번 시작 값이 일부만 있으면 날짜 줄은 보이지만 다시 읽히지 않는다(앞 날짜 유지).
    for scene, line in (
        ({"date_mode": "calendar", "year": 2026}, "날짜: 2026년"),
        ({"date_mode": "world", "era": "대한"}, "날짜: 대한"),
    ):
        (shown,) = format_info_lines(scene, [])
        assert shown == line
        assert parse_info(shown, scene["date_mode"], [])["date"] is None


def test_chat_us_08_scene_view_has_info_shape_and_every_character():
    assert scene_view(SCENE, STATES) == {
        "date": {
            "mode": "calendar",
            "era": None,
            "year": 2026,
            "month": 11,
            "day": 22,
            "reference_event": None,
            "elapsed_day": None,
        },
        "time": {"hour": 21, "minute": 5},
        "place": "경찰서 옥상",
        "characters": STATES,
    }
    assert scene_view(None, []) == {"date": None, "time": None, "place": None, "characters": []}
    # 날짜 방식만 있고 값이 없으면 date는 null이다.
    assert scene_view({"date_mode": "elapsed"}, [])["date"] is None
