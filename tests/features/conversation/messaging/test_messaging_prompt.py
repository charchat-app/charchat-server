"""CHAT-US-02 모델 요청 구성(KAN-17 AC11, KAN-16 AC27·AC28·AC29)."""

from messaging_helpers import send, session_characters, user_part

from app.features.conversation.messaging.prompt import (
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
    assert messages[3]["content"] == "[민서] 지금 입력"
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
        "대화 구성: 대화 중심",
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
        "문체: 하드보일드",
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
    assert messages[3] == {"role": "user", "content": "[민서] 두 번째"}
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
