"""CHAT-US-03 테스트 도우미(저장된 스토리 준비·요청 보내기·가짜 공급자)."""

import threading

STORIES = "/api/v1/conversation/stories"
SESSIONS = "/api/v1/conversation/sessions"
GENERATIONS = "/api/v1/conversation/generations"
AUTHOR = {"X-User-Id": "1"}
PLAYER = {"X-User-Id": "2"}


def seed_story(db, title: str | None = "비 오는 도시", state: str = "public"):
    """대화 시작이 가능한 저장 스토리(장면·관계·호감도·프리셋). 반환: (story, intro, preset)."""
    story = db.execute(
        """INSERT INTO stories (owner_id, title, subtitle, world_description, save_state)
           VALUES (1, %s, '한 줄 소개', '세계관', %s) RETURNING id""",
        (title, state),
    ).fetchone()["id"]
    first = db.execute(
        """INSERT INTO characters (story_id, name, description, position)
           VALUES (%s, '도경', '형사', 0) RETURNING id""",
        (story,),
    ).fetchone()["id"]
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
    preset = db.execute(
        """WITH image AS (
               INSERT INTO images (owner_id, storage_key, mime_type, byte_size)
               VALUES (1, %s, 'image/png', 1) RETURNING id
           )
           INSERT INTO story_user_presets (story_id, name, image_id, description, position)
           SELECT %s, '{{user}}', id, '신입 탐정', 0 FROM image RETURNING id""",
        (f"story-{story}", story),
    ).fetchone()["id"]
    return story, intro, preset


def start(client, story, *, headers=PLAYER, play_name="민서") -> int:
    story_id, intro, preset = story
    response = client.post(
        f"{STORIES}/{story_id}/sessions",
        headers=headers,
        json={
            "intro_id": intro,
            "profile_kind": "preset",
            "profile_id": preset,
            "play_name": play_name,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def send(client, session_id, body, request_id="req-00000001", headers=PLAYER):
    return client.post(
        f"{SESSIONS}/{session_id}/messages",
        headers=headers,
        json={
            "request_id": request_id,
            "parts": [{"speaker_kind": "user", "character_id": None, "body": body}],
        },
    )


def lookup(client, session_id, request_id, headers=PLAYER):
    return client.get(
        f"{SESSIONS}/{session_id}/generations",
        headers=headers,
        params={"request_id": request_id},
    )


def count(db, table: str) -> int:
    return db.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]


class GatedProvider:
    """첫 조각을 보낸 뒤 release()까지 멈추는 가짜 공급자(외부 호출 없음)."""

    def __init__(self, first: str, rest: str):
        self.first = first
        self.rest = rest
        self.gate = threading.Event()
        self.calls = 0

    def release(self):
        self.gate.set()

    def stream(self, request):
        self.calls += 1
        yield self.first
        assert self.gate.wait(10), "테스트가 생성을 풀어 주지 않았습니다."
        yield self.rest
