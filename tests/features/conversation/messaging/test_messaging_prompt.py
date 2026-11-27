"""CHAT-US-02 모델 요청 구성(KAN-17 AC11, KAN-16 AC27·AC28·AC29), CHAT-US-06 표현 지침."""

import pytest
from messaging_helpers import send, session_characters, user_part

from app.features.conversation.messaging.prompt import (
    COMPOSITION_GUIDE,
    POINT_OF_VIEW_GUIDE,
    STYLE_GUIDE,
    TENSE_GUIDE,
    PromptContext,
    PromptHistoryMessage,
    PromptPart,
    build_messages,
)


def positions(text, *needles):
    found = [text.index(needle) for needle in needles]
    assert found == sorted(found), list(zip(needles, found, strict=True))
    return found


def minimal_context(**overrides):
    values: dict = dict(
        title="제목",
        subtitle="소개",
        world_description="세계",
        tags=["태그"],
        characters=[{"name": "도경", "description": "형사"}],
        expression={},
        fewshots=[[PromptPart("user", None, "{{user}} 예시")]],
        intro=[PromptPart("narrator", None, "인트로")],
        play_name="민서",
        profile_description="탐정",
        scene={"place": "옥상"},
        character_states=[],
        affinities=[],
        relationships=[],
        session_character_names=["도경"],
        current=[PromptPart("user", "민서", "지금 입력")],
    )
    values.update(overrides)
    return PromptContext(**values)


def test_kan16_ac27_ac28_pure_first_turn_order():
    messages = build_messages(minimal_context())
    assert [m["role"] for m in messages] == ["system", "user"]
    positions(
        messages[0]["content"],
        "## 지침",
        "## 기본 정보",
        "## 표현 설정",
        "## 이용자 캐릭터",
        "## 호감도·관계",
        "## 출력 형식",
    )
    first = messages[1]["content"]
    positions(first, "[참고 예시", "[민서] 민서 예시", "[참고 예시 끝]", "[지금까지의 이야기]")
    positions(first, "[지금까지의 이야기]", "[나레이터] 인트로", "[현재 장면]", "장소: 옥상")
    assert first.endswith("[이용자 입력]\n[민서] 지금 입력")
    assert "{{user}}" not in first


def test_chat_us_02_ac11_pure_later_turn_history_then_new_input():
    history = [
        PromptHistoryMessage("user", [PromptPart("user", "민서", "처음")]),
        PromptHistoryMessage(
            "assistant",
            [PromptPart("character", "도경", "답"), PromptPart("narrator", None, "서술")],
        ),
    ]
    messages = build_messages(minimal_context(history=history))
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert messages[1]["content"].endswith("[이용자 입력]\n[민서] 처음")
    assert messages[2]["content"] == "@도경: 답\n@나레이터: 서술"
    # CHAT-US-08: 이후 턴에는 현재 장면이 첫 블록이 아니라 새 입력 바로 앞에 붙는다.
    assert "[현재 장면]" not in messages[1]["content"]
    assert messages[3]["content"] == "[현재 장면]\n장소: 옥상\n\n[이용자 입력]\n[민서] 지금 입력"
    # 이용자 입력의 {{user}}는 바꾸지 않는다.
    later = build_messages(minimal_context(current=[PromptPart("user", "민서", "{{user}}")]))
    assert later[1]["content"].endswith("[민서] {{user}}")


def test_kan16_ac27_ac28_first_request_uses_saved_settings_in_order(client, db, session, recorder):
    chars = session_characters(client, session)
    parts = [
        user_part("첫 입력"),
        {"speaker_kind": "character", "character_id": chars["Bea"], "body": "Bea 대사"},
        {"speaker_kind": "narrator", "character_id": None, "body": "*바람이 분다.*"},
    ]
    assert send(client, session, parts).status_code == 202
    (request,) = recorder.requests
    # 세션에 고정된 기본 모델(루나)의 공급자 쪽 ID로 호출한다.
    assert (request.provider, request.model) == ("openai", "gpt-6-luna")
    messages = request.messages
    assert [m["role"] for m in messages] == ["system", "user"]
    system = messages[0]["content"]
    positions(
        system,
        "## 지침",
        "제목: 비 오는 도시",
        "한 줄 소개: 한 줄 소개",
        "세계관: 세계관 설명",
        "해시태그: #느와르",
        "- 도경: 무뚝뚝한 형사",
        "- Bea: 수다스러운 동료",
        "시점: 1인칭",
        "시제: 현재형",
        "대화 구성: 대사 중심",
        "문체: 서정적",
        "응답 길이: 짧게",
        "## 이용자 캐릭터",
        "이름: 민서",
        "상세: 신입 탐정",
        "## 호감도·관계",
        "- 도경 → 민서: 호감도 10",
        "- 도경 → 민서: 관계 파트너",
        "## 출력 형식",
        "등장 캐릭터(도경, Bea) 또는 나레이터",
    )
    first = messages[1]["content"]
    positions(
        first,
        "[참고 예시",
        "[민서] 안녕 민서입니다",
        "[도경] *고개를 든다.* 늦었네.",
        "[참고 예시 끝]",
        "[지금까지의 이야기]",
        "[나레이터] 비가 내린다.",
        "[도경] 왔어, 민서?",
        "[현재 장면]",
        "장소: 경찰서 옥상",
        "날짜: 2026년 11월 22일",
        "시각: 21:05",
        "도경 착장: 젖은 코트",
        "도경 속마음: 피곤하다",
        "[이용자 입력]",
    )
    assert first.endswith(
        "[이용자 입력]\n[민서] 첫 입력\n[Bea] Bea 대사\n[나레이터] *바람이 분다.*"
    )
    assert "{{user}}" not in first


def test_kan16_ac29_chat_us_02_ac11_second_request_rereads_latest_saved_settings(
    client, db, saved_story, session, recorder
):
    story, _, (first_char, _) = saved_story
    assert send(client, session, [user_part("첫 입력")], request_id="turn-0001").status_code == 202

    # 다음 요청 전에 제작자가 설정을 저장하고, 세션 현재값도 바뀐다.
    db.execute(
        "UPDATE stories SET title = '맑은 도시', world_description = '새 세계관' WHERE id = %s",
        (story,),
    )
    db.execute("UPDATE characters SET description = '다정한 형사' WHERE id = %s", (first_char,))
    db.execute(
        """UPDATE story_expression_settings
           SET response_length = 'long', style_mode = 'custom', preset_styles = '{}',
               custom_style = '하드보일드' WHERE story_id = %s""",
        (story,),
    )
    db.execute("UPDATE fewshot_messages SET body = '새 예시 {{user}}' WHERE position = 0")
    tag = db.execute("INSERT INTO tags (name) VALUES ('추리') RETURNING id").fetchone()["id"]
    db.execute("INSERT INTO story_tags VALUES (%s, %s)", (story, tag))
    db.execute("UPDATE conversation_affinities SET affinity = 42 WHERE session_id = %s", (session,))
    # 저장하지 않은 임시 저장본은 모델 요청에 쓰지 않는다.
    db.execute(
        """INSERT INTO story_working_copies (story_id, creation, creation_saved_at)
           VALUES (%s, '{"title": "임시 제목"}', now())""",
        (story,),
    )

    assert send(client, session, [user_part("두 번째")], request_id="turn-0002").status_code == 202
    first_request, second_request = recorder.requests
    messages = second_request.messages
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    system = messages[0]["content"]
    for expected in (
        "제목: 맑은 도시",
        "세계관: 새 세계관",
        "#느와르 #추리",
        "- 도경: 다정한 형사",
        "문체(제작자 지침):\n하드보일드",
        "응답 길이: 길게",
        "- 도경 → 민서: 호감도 42",
    ):
        assert expected in system
    for stale in ("비 오는 도시", "무뚝뚝한 형사", "서정적", "호감도 10", "임시 제목"):
        assert stale not in system
    assert "[민서] 새 예시 민서" in messages[1]["content"]
    assert messages[1]["content"].endswith("[이용자 입력]\n[민서] 첫 입력")
    # 저장된 이전 응답이 발화자·순서를 유지한 이력으로 들어간다(AC09·AC11).
    assert messages[2] == {"role": "assistant", "content": "@도경: 응답 1번\n@나레이터: 끝"}
    assert messages[3]["role"] == "user"
    assert messages[3]["content"].startswith("[현재 장면]\n장소: 경찰서 옥상")
    assert messages[3]["content"].endswith("[이용자 입력]\n[민서] 두 번째")
    # 첫 요청은 당시 설정 그대로였다.
    assert "제목: 비 오는 도시" in first_request.messages[0]["content"]


def test_chat_us_02_ac09_ac17_failed_partial_text_not_in_next_history(
    client, db, session, recorder
):
    from app.features.conversation.messaging.providers import ProviderError

    recorder.chunks = ["@도경: 일부만", " 보였다"]
    recorder.error = ProviderError("STREAM_INTERRUPTED")
    recorder.error_after = 1
    send(client, session, [user_part("첫 입력")], request_id="turn-0001")
    recorder.chunks = None
    recorder.error = None
    send(client, session, [user_part("두 번째")], request_id="turn-0002")
    messages = recorder.requests[-1].messages
    assert [m["role"] for m in messages] == ["system", "user", "user"]
    assert all("일부만" not in m["content"] for m in messages)


def system_for(**expression):
    return build_messages(minimal_context(expression=expression))[0]["content"]


@pytest.mark.parametrize(
    "code, label",
    [("first_person", "1인칭"), ("second_person", "2인칭"), ("third_person", "3인칭")],
)
def test_chat_us_06_ac06_point_of_view_sends_meaning_for_all_characters(code, label):
    system = system_for(point_of_view=code)
    assert f"시점: {label} — {POINT_OF_VIEW_GUIDE[code]}" in system
    for other, guide in POINT_OF_VIEW_GUIDE.items():
        if other != code:
            assert guide not in system
    # 시점은 모든 등장 캐릭터의 지문에 적용하고 대사 속 인칭은 바꾸지 않는다.
    assert "모든 등장 캐릭터의 지문에 적용한다" in system
    assert "서술 시점에 맞춰 바꾸지 않는다" in system


def test_chat_us_06_ac06_second_person_does_not_decide_user_actions():
    system = system_for(point_of_view="second_person")
    assert "이용자 캐릭터를 가리킬 때는 이름 대신 '너' 또는 '당신'으로 쓰며" in system
    assert "지문에서는 이용자 캐릭터 이름(민서) 대신 '너' 또는 '당신'으로 부른다." in system
    # 출력 형식 예시도 2인칭 지문으로 보여 준다.
    assert "*그가 창밖을 보다가 너를 돌아보았다.*" in system
    assert "이름(민서) 대신" not in system_for(point_of_view="third_person")
    no_name = build_messages(
        minimal_context(play_name="", expression={"point_of_view": "second_person"})
    )[0]["content"]
    assert "이름() 대신" not in no_name
    assert "이용자 캐릭터의 행동·감정을 정하지 않는다" in system


@pytest.mark.parametrize(
    "code, label, narration", [("past", "과거형", "바라보았다"), ("present", "현재형", "바라본다")]
)
def test_chat_us_06_ac07_tense_sends_meaning_and_matching_format_example(code, label, narration):
    system = system_for(tense=code)
    assert f"시제: {label} — {TENSE_GUIDE[code]}" in system
    other = "present" if code == "past" else "past"
    assert TENSE_GUIDE[other] not in system
    assert "대사는 말하는 내용에 맞는 시제를 쓰고 서술 시제에 맞춰 바꾸지 않는다" in system
    # 출력 형식 예시도 고른 시제로 쓴다(예시가 설정과 어긋나지 않게).
    assert f"*그는 창밖을 {narration}.*" in system


def test_chat_us_06_ac06_ac07_ac11_ac13_default_expression_is_third_person_past_balanced_normal():
    system = system_for()
    for expected in ("시점: 3인칭", "시제: 과거형", "대화 구성: 밸런스형", "응답 길이: 보통"):
        assert expected in system


def test_chat_us_06_ac08_two_preset_styles_send_both_guides_only():
    system = system_for(style_mode="preset", preset_styles=["lyrical", "tense"])
    assert "문체: 서정적인, 긴장감 있는" in system
    assert f"  - 서정적인: {STYLE_GUIDE['lyrical']}" in system
    assert f"  - 긴장감 있는: {STYLE_GUIDE['tense']}" in system
    for code, guide in STYLE_GUIDE.items():
        if code not in ("lyrical", "tense"):
            assert guide not in system
    assert "문체를 적용해도 캐릭터의 성격·고유 말투는 바꾸지 않는다" in system
    # 문체를 서술에만 묶지 않는다(설레는·섬세한 가이드는 대사·말끝도 다룬다).
    assert "서술의 분위기만" not in system


@pytest.mark.parametrize("code", sorted(STYLE_GUIDE))
def test_chat_us_06_ac08_every_preset_style_has_its_guide(code):
    assert STYLE_GUIDE[code] in system_for(style_mode="preset", preset_styles=[code])


def test_chat_us_06_ac09_custom_style_sends_creator_text_without_preset_guides():
    system = system_for(
        style_mode="custom", preset_styles=["lyrical"], custom_style="짧은 문장, 건조한 하드보일드"
    )
    assert "문체(제작자 지침):\n짧은 문장, 건조한 하드보일드\n" in system
    assert "  문체를 적용해도 캐릭터의 성격·고유 말투는 바꾸지 않는다." in system
    for guide in STYLE_GUIDE.values():
        assert guide not in system
    assert "서정적인" not in system


def test_chat_us_06_ac10_no_style_adds_no_style_guide():
    system = system_for(style_mode="none", preset_styles=[], custom_style=None)
    assert "문체:" not in system
    assert "지정 없음" not in system.split("## 표현 설정")[1].split("## 이용자 캐릭터")[0]
    for guide in STYLE_GUIDE.values():
        assert guide not in system
    # 퓨샷이 없어도 시점·시제 지침은 그대로 보낸다.
    messages = build_messages(minimal_context(fewshots=[]))
    assert "[참고 예시" not in messages[1]["content"]
    assert "시점: 3인칭" in messages[0]["content"]


@pytest.mark.parametrize(
    "code, label, ratio",
    [
        ("dialogue", "대사 중심", "대사 70 : 서술 30"),
        ("balanced", "밸런스형", "대사 50 : 서술 50"),
        ("action", "행동 중심", "대사 30 : 서술 70"),
    ],
)
def test_chat_us_06_ac11_composition_sends_guide_and_soft_ratio(code, label, ratio):
    system = system_for(composition=code)
    assert f"대화 구성: {label} — {COMPOSITION_GUIDE[code]}" in system
    assert ratio in system
    assert "꼭 맞출 필요는 없다" in system
    assert "억지로 줄이거나 불필요하게 늘리지 않는다" in system


def test_chat_us_06_ac12_output_format_blank_line_between_narration_and_dialogue():
    system = system_for(tense="past")
    fmt = system.split("## 출력 형식")[1]
    assert "대사는 큰따옴표 없이 일반 글로 쓴다" in fmt
    assert "항상 줄바꿈 두 번으로 빈 줄 하나를 둔다" in fmt
    assert "한 줄에 서술과 대사를 섞지 않는다" in fmt
    assert (
        "@캐릭터 이름: *그는 창밖을 바라보았다.*\n\n오늘은 비가 오네.\n\n*그는 우산을 집어 들었다.*"
        in fmt
    )
    assert '"' not in fmt.split("예시:")[1]


@pytest.mark.parametrize(
    "code, label, target",
    [
        ("short", "짧게", "300~500자"),
        ("normal", "보통", "500~1000자"),
        ("long", "길게", "1000~2000자"),
    ],
)
def test_chat_us_06_ac13_response_length_sends_initial_target(code, label, target):
    system = system_for(response_length=code)
    assert f"응답 길이: {label} — 응답 전체(대사·서술 합계) {target}를 목표로 한다." in system
    assert "중간에 끊지 말고 자연스럽게 마무리한다" in system
    # 시험에서 목표보다 짧게 끝나는 경향이 있어 덜 쓰지 말라는 안내도 보낸다.
    assert "목표보다 한참 짧게 끝내지 않는다" in system
    assert "같은 말을 되풀이하지 말고 행동·반응·장면을 더 전개한다" in system


def test_chat_us_06_ac14_fewshot_is_reference_and_settings_win():
    messages = build_messages(minimal_context())
    system, first = messages[0]["content"], messages[1]["content"]
    assert "아래 설정은 참고 예시보다 우선한다." in system
    assert "참고 예시는 실제로 일어난 사건이 아니다." in system
    assert first.startswith(
        "[참고 예시 — 실제로 일어난 일이 아니다. 표현 설정(시점·시제·문체·대화 구성)을 따르고, "
        "그 안에서 말투·상호작용·서술 표현만 참고한다]"
    )


def test_chat_us_06_ac01_ac03_ac04_ac05_guardrail_rules():
    system = build_messages(minimal_context())[0]["content"]
    guide = system.split("## 기본 정보")[0]
    # AC01: 캐릭터마다 성격·고유 말투 유지
    assert "각 캐릭터는 설정된 성격·고유 말투를 지킨다" in guide
    # AC03: 과거 사실과 현재 시점 구분, 새 전개 허용
    assert "과거 사실은 바꾸지 않고" in guide
    assert "세계관에 없는 새 사건·상황도 이야기의 다음 전개로 받아들여" in guide
    # AC04: 바뀐 호칭·말높임 유지
    assert "처음 표현으로 매번 되돌리지 않는다" in guide
    # AC05: 상대별 관계·호감도 → 호칭·어조·거리감
    assert "상대마다 맞는 호칭·어조·거리감·태도로 말한다" in guide


def test_chat_us_06_pm_request_user_written_character_lines_are_not_repeated():
    # AC 표에는 없는 나래님 요청(11/27, 제타의 유저 발언권 문제)이다.
    """이용자가 캐릭터·나레이터 줄을 대신 쓰면 이미 일어난 전개로 보고 되풀이하지 않는다."""
    messages = build_messages(
        minimal_context(
            current=[
                PromptPart("user", None, "같이 가자"),
                PromptPart("character", "도경", "그래, 가자."),
            ]
        )
    )
    guide = messages[0]["content"].split("## 기본 정보")[0]
    assert "이용자 캐릭터(민서)의 대사·행동·생각·감정은 이용자만 정한다" in guide
    assert "이용자가 그 인물을 대신 써서 이미 정한" in guide
    assert "되풀이하지 말고, 그 바로 다음부터 이어 쓴다" in guide
    assert messages[1]["content"].endswith("[민서] 같이 가자\n[도경] 그래, 가자.")


def test_chat_us_06_guides_pin_the_design_meaning_literally():
    """가이드 상수를 비우거나 뜻을 바꾸면 실패하도록 핵심 문구를 그대로 고정한다(AC06·07·08·11)."""
    first = system_for(point_of_view="first_person")
    assert "지문은 서술하는 캐릭터가 자신을 '나'로 표현한다" in first
    assert "지금 누구의 '나'인지 구별되게 쓴다" in first
    assert "나레이터 블록은 어느 캐릭터의 '나'도 아닌 짧은 장면 전환에만 쓴다" in first
    third = system_for(point_of_view="third_person")
    assert "인물 바깥에서 등장 캐릭터들의 행동과 상황을 이름이나 그·그녀 같은 대명사로" in third
    assert "행동·상황 서술을 과거형(~했다)으로 쓴다. 예: 물컵을 내밀었다." in system_for(
        tense="past"
    )
    assert "행동·상황 서술을 현재형(~한다)으로 쓴다. 예: 물컵을 내민다." in system_for(
        tense="present"
    )
    styles = system_for(style_mode="preset", preset_styles=["romantic", "warm"])
    assert "설정에 없는 호감이나 연애 관계를 사실로 확정하지는 않는다" in styles
    assert "기존 갈등이나 인물 성격을 지우지는 않는다" in styles
    assert "대사로 장면을 이끌고 행동·상황 서술은 보조한다" in system_for(composition="dialogue")
    assert "행동·표정·움직임·장면 변화로 장면을 이끌고" in system_for(composition="action")


@pytest.mark.parametrize(
    "code, phrase",
    [
        (
            "dynamic",
            "힘 있는 동사와 빠른 문장 호흡, 속도감 있는 표현으로 생동감과 박진감을 살린다.",
        ),
        ("lyrical", "비유와 감각적인 어휘, 리듬 있는 문장으로 장면과 감정의 여운을 살린다."),
        ("tense", "정보를 조금씩 드러내고, 침묵과 문장의 완급으로 긴박함과 묘한 긴장감을 살린다."),
        ("humorous", "진지한 상황에 억지로 농담을 넣지는 않는다."),
        ("plain", "감정을 없애거나 무조건 짧게 쓰지는 않는다."),
        ("delicate", "감정을 단정하기보다 미묘한 변화를 드러낸다."),
        ("romantic", "읽는 사람이 두근거리는 로맨스풍 문체를 만든다."),
        ("warm", "일상의 감각 묘사로 다정함과 온기를 살린다."),
    ],
)
def test_chat_us_06_ac08_every_style_guide_matches_design_doc(code, phrase):
    assert phrase in system_for(style_mode="preset", preset_styles=[code])


def test_chat_us_06_ac11_balanced_guide_literal():
    assert "장면에 맞게 대사와 행동·상황 서술을 고르게 쓴다" in system_for(composition="balanced")


def test_chat_us_06_ac06_first_person_format_example_uses_na():
    assert "*나는 창밖을 바라보았다.*" in system_for(point_of_view="first_person")


def test_chat_us_08_ac01_guardrail_keeps_scene_until_change_is_stated():
    guide = build_messages(minimal_context())[0]["content"].split("## 기본 정보")[0]
    assert "[현재 장면]은 지금의 날짜·시각·장소와 캐릭터별 착장·속마음이다" in guide
    assert "근거 없이 다른 장소나 시간대로" in guide
    assert "장면을 멈춰 두지 않고 대화는 자연스럽게 진행한다" in guide


def test_chat_us_08_user_edit_guardrail_follows_current_scene_of_user_character():
    """장면의 기준은 이용자 캐릭터다. 직접 수정 알림은 지침이 아니라 그때의 장면 블록에만 붙는다
    (판단이 놓친 이동까지 '장면을 따르라'로 되돌리지 않게)."""
    guide = build_messages(minimal_context())[0]["content"].split("## 기본 정보")[0]
    assert "[현재 장면]은 이용자 캐릭터가 있는 곳 기준이다." in guide
    assert "앞 대화와 다르면" not in guide
    assert "직접 고쳤" not in guide


USER_EDITED_NOTE = "※ 이용자가 방금 이 장면을 직접 고쳤다. 앞 대화와 달라도 이 장면에서 이어 간다."


def test_chat_us_08_user_edit_note_ends_scene_block_only_when_edited():
    states = [{"name": "도경", "clothing": "회색 셔츠", "inner_thought": None}]
    first = build_messages(minimal_context(character_states=states, scene_user_edited=True))
    scene = first[1]["content"].split("[현재 장면]\n")[1].split("\n\n")[0]
    assert scene.endswith("도경 착장: 회색 셔츠\n" + USER_EDITED_NOTE)
    assert USER_EDITED_NOTE not in first[0]["content"]
    # 이후 턴: 새 입력 바로 앞 [현재 장면] 블록 끝, [이용자 입력] 앞.
    history = [
        PromptHistoryMessage("user", [PromptPart("user", "민서", "앞 입력")]),
        PromptHistoryMessage("assistant", [PromptPart("narrator", None, "앞 응답")]),
    ]
    later = build_messages(minimal_context(scene_user_edited=True, history=history))
    assert later[-1]["content"] == (
        "[현재 장면]\n장소: 옥상\n" + USER_EDITED_NOTE + "\n\n[이용자 입력]\n[민서] 지금 입력"
    )
    # 고치지 않았으면 어디에도 없다.
    for messages in (
        build_messages(minimal_context()),
        build_messages(minimal_context(history=history)),
    ):
        assert all(USER_EDITED_NOTE not in m["content"] for m in messages)


def test_chat_us_08_guardrail_uses_clothing_and_inner_thought_as_current_state():
    guide = build_messages(minimal_context())[0]["content"].split("## 기본 정보")[0]
    assert (
        "착장과 속마음은 단순 정보가 아니라 응답에 반영할 지금의 상태다. 캐릭터의 행동·묘사는 "
        "지금 입은 옷과 어긋나지 않게 쓰고, 속마음은 그 캐릭터의 말투·태도·망설임·행동에 드러나게 "
        "한다. 속마음을 설명하듯 그대로 말하지는 않는다."
    ) in guide
    # 장면 지침 바로 다음 줄이다.
    lines = guide.splitlines()
    scene_line = next(i for i, line in enumerate(lines) if line.startswith("- [현재 장면]은"))
    assert lines[scene_line + 1].startswith("- 착장과 속마음은 단순 정보가 아니라")


def test_chat_us_08_scene_block_lists_clothing_and_inner_thought_of_every_character():
    states = [
        {"name": "도경", "clothing": "젖은 코트", "inner_thought": "피곤하다"},
        {"name": "Bea", "clothing": "노란 우비", "inner_thought": "도경이 걱정된다"},
    ]
    block = build_messages(minimal_context(character_states=states))[1]["content"]
    scene = block.split("[현재 장면]")[1]
    for line in (
        "도경 착장: 젖은 코트",
        "도경 속마음: 피곤하다",
        "Bea 착장: 노란 우비",
        "Bea 속마음: 도경이 걱정된다",
    ):
        assert line in scene


def test_chat_us_08_scene_block_has_no_invented_defaults():
    hour_only = build_messages(minimal_context(scene={"place": "옥상", "hour": 21, "minute": None}))
    assert "시각: 21시" in hour_only[1]["content"]
    assert "21:00" not in hour_only[1]["content"]
    elapsed = build_messages(
        minimal_context(scene={"date_mode": "elapsed", "reference_event": None, "elapsed_day": 3})
    )
    assert "날짜: 3일째" in elapsed[1]["content"]
    assert "시작 이후" not in elapsed[1]["content"]
    with_event = build_messages(
        minimal_context(scene={"date_mode": "elapsed", "reference_event": "감금", "elapsed_day": 3})
    )
    assert "날짜: 감금 이후 3일째" in with_event[1]["content"]
