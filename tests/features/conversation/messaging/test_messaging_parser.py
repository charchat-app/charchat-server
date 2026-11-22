"""CHAT-US-02 응답 원문 파서. 앱 parseAssistantText와 같은 공유 사례를 확인한다(AC03·AC09)."""

from app.features.conversation.messaging.parser import ParsedPart, parse_assistant_text

NAMES = ["도경", "Bea"]


def character(name, body):
    return ParsedPart("character", name, body)


def narrator(body):
    return ParsedPart("narrator", None, body)


def test_chat_us_02_ac09_shared_case1_single_character_tag():
    assert parse_assistant_text("@도경: 안녕", NAMES) == [character("도경", "안녕")]


def test_chat_us_02_ac09_shared_case2_untagged_text_is_narrator():
    assert parse_assistant_text("그냥 텍스트", NAMES) == [narrator("그냥 텍스트")]


def test_chat_us_02_ac09_shared_case3_order_and_consecutive_speakers_kept():
    text = "@도경: 하나\n@나레이터: 둘\n@도경: 셋\n@도경: 넷"
    assert parse_assistant_text(text, NAMES) == [
        character("도경", "하나"),
        narrator("둘"),
        character("도경", "셋"),
        character("도경", "넷"),
    ]


def test_chat_us_02_ac09_shared_case4_unknown_name_kept_verbatim_as_narrator():
    assert parse_assistant_text("@모르는사람: 말", NAMES) == [narrator("@모르는사람: 말")]


def test_chat_us_02_ac09_shared_case5_multiline_body():
    assert parse_assistant_text("@도경: 첫줄\n둘째줄", NAMES) == [character("도경", "첫줄\n둘째줄")]


def test_chat_us_02_ac09_shared_case6_incomplete_trailing_tag_stays_in_previous_part():
    assert parse_assistant_text("@도경: 안녕\n@나", NAMES) == [character("도경", "안녕\n@나")]
    assert parse_assistant_text("@도경: 안녕\n@나레이터: 끝", NAMES) == [
        character("도경", "안녕"),
        narrator("끝"),
    ]


def test_chat_us_02_ac09_text_before_first_tag_and_user_tag_become_narrator():
    text = "서두\n@도경: 대사\n@민서: 내가 말한다\n@Bea: *웃는다* **정말?**"
    assert parse_assistant_text(text, NAMES) == [
        narrator("서두"),
        character("도경", "대사"),
        narrator("@민서: 내가 말한다"),
        character("Bea", "*웃는다* **정말?**"),
    ]


def test_chat_us_02_ac09_blank_parts_dropped_and_blank_lines_trimmed():
    assert parse_assistant_text("\n@도경:\n@나레이터: 비\n\n", NAMES) == [narrator("비")]
    assert parse_assistant_text("   ", NAMES) == []
