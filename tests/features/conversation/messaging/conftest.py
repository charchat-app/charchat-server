"""CHAT-US-02 테스트 공통 준비: 저장된 스토리·세션과 결정적인 생성 실행.

기본은 생성을 요청 처리 안에서 끝까지 실행(inline)해 결과를 바로 확인한다.
실제 스레드·롱 폴링 경로는 `thread_worker` 픽스처를 쓰는 테스트에서 따로 확인한다.
"""

import pytest
from messaging_helpers import RecordingProvider, start_session

from app.core.config import settings
from app.features.conversation.messaging import providers


@pytest.fixture(autouse=True)
def inline_worker(monkeypatch):
    monkeypatch.setattr(settings, "chat_generation_worker", "inline")
    monkeypatch.setattr(settings, "chat_fake_chunk_delay_ms", 0)
    monkeypatch.setattr(settings, "chat_fake_fail_mode", "none")
    monkeypatch.setattr(settings, "chat_llm_provider", "fake")
    monkeypatch.setattr(settings, "chat_default_model_name", "루나")


@pytest.fixture
def thread_worker(monkeypatch):
    monkeypatch.setattr(settings, "chat_generation_worker", "thread")
    monkeypatch.setattr(settings, "chat_fake_chunk_delay_ms", 20)


@pytest.fixture
def recorder(monkeypatch):
    provider = RecordingProvider()
    monkeypatch.setattr(providers, "get_provider", lambda: provider)
    return provider


@pytest.fixture
def saved_story(db):
    """대화 시작이 가능한 공개 스토리. 반환: (story_id, intro_id, character ids)."""
    story = db.execute(
        """INSERT INTO stories (owner_id, title, subtitle, world_description, save_state)
           VALUES (1, '비 오는 도시', '한 줄 소개', '세계관 설명', 'public') RETURNING id"""
    ).fetchone()["id"]
    chars = db.execute(
        """INSERT INTO characters (story_id, name, description, position)
           VALUES (%s, '도경', '무뚝뚝한 형사', 0), (%s, 'Bea', '수다스러운 동료', 1)
           RETURNING id""",
        (story, story),
    ).fetchall()
    first, second = (row["id"] for row in chars)
    intro = db.execute(
        "INSERT INTO intros (story_id, title, position) VALUES (%s, '시작', 0) RETURNING id",
        (story,),
    ).fetchone()["id"]
    db.execute(
        """INSERT INTO intro_messages (intro_id, speaker_kind, character_id, body, position)
           VALUES (%s, 'narrator', NULL, '비가 내린다.', 0),
                  (%s, 'character', %s, '왔어, {{user}}?', 1)""",
        (intro, intro, first),
    )
    db.execute(
        """INSERT INTO intro_scene_settings (intro_id, date_mode, year_input, month_input,
                                             day_input, time_format, hour_input, minute_input,
                                             place)
           VALUES (%s, 'calendar', '2026', '11', '22', '24h', '21', '05', '경찰서 옥상')""",
        (intro,),
    )
    db.execute(
        "INSERT INTO intro_character_states VALUES (%s, %s, '젖은 코트', '피곤하다')",
        (intro, first),
    )
    db.execute(
        """INSERT INTO character_initial_affinities
               (source_character_id, target_kind, target_character_id, affinity_input)
           VALUES (%s, 'user', NULL, '10')""",
        (first,),
    )
    db.execute(
        """INSERT INTO character_initial_relationships
               (source_kind, source_character_id, target_kind, target_character_id, label)
           VALUES ('character', %s, 'user', NULL, '파트너')""",
        (first,),
    )
    db.execute(
        """INSERT INTO story_expression_settings
               (story_id, point_of_view, tense, composition, response_length, style_mode,
                preset_styles)
           VALUES (%s, 'first_person', 'present', 'dialogue', 'short', 'preset',
                   ARRAY['lyrical'])""",
        (story,),
    )
    fewshot = db.execute(
        "INSERT INTO fewshots (story_id, position) VALUES (%s, 0) RETURNING id", (story,)
    ).fetchone()["id"]
    db.execute(
        """INSERT INTO fewshot_messages (fewshot_id, speaker_kind, character_id, body, position)
           VALUES (%s, 'user', NULL, '안녕 {{user}}입니다', 0),
                  (%s, 'character', %s, '*고개를 든다.* 늦었네.', 1)""",
        (fewshot, fewshot, first),
    )
    tag = db.execute("INSERT INTO tags (name) VALUES ('느와르') RETURNING id").fetchone()["id"]
    db.execute("INSERT INTO story_tags VALUES (%s, %s)", (story, tag))
    image = db.execute(
        """INSERT INTO images (owner_id, storage_key, mime_type, byte_size)
           VALUES (1, 'sample', 'image/png', 1) RETURNING id"""
    ).fetchone()["id"]
    db.execute(
        """INSERT INTO story_user_presets (story_id, name, image_id, description, position)
           VALUES (%s, '{{user}}', %s, '신입 탐정', 0)""",
        (story, image),
    )
    db.execute("INSERT INTO user_play_settings VALUES (1, '작가'), (2, '민서')")
    return story, intro, (first, second)


@pytest.fixture
def session(client, saved_story):
    story, intro, _ = saved_story
    return start_session(client, story, intro)
