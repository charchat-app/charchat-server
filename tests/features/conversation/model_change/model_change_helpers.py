"""CHAT-US-13 테스트 도우미(저장 스토리 준비·요청 보내기·가짜 공급자). 외부 호출 없음."""

STORIES = "/api/v1/conversation/stories"
SESSIONS = "/api/v1/conversation/sessions"
GENERATIONS = "/api/v1/conversation/generations"
MODELS = "/api/v1/conversation/models"
AUTHOR = {"X-User-Id": "1"}
PLAYER = {"X-User-Id": "2"}


def seed_story(db):
    """대화 시작이 가능한 공개 스토리(캐릭터·장면·착장·호감도·관계·프리셋). 반환: (story, intro)."""
    story = db.execute(
        """INSERT INTO stories (owner_id, title, subtitle, world_description, save_state)
           VALUES (1, '비 오는 도시', '한 줄 소개', '세계관 설명', 'public') RETURNING id"""
    ).fetchone()["id"]
    first, _second = (
        row["id"]
        for row in db.execute(
            """INSERT INTO characters (story_id, name, description, position)
               VALUES (%s, '도경', '무뚝뚝한 형사', 0), (%s, 'Bea', '수다스러운 동료', 1)
               RETURNING id""",
            (story, story),
        ).fetchall()
    )
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
    return story, intro


class RecordingProvider:
    """모델 요청을 기록하고 정해진 응답 조각을 보낸다. 실제 모델 ID는 알려 주지 않는다."""

    def __init__(self, error=None, error_after=None):
        self.requests = []
        self.error = error
        self.error_after = error_after

    def stream(self, request):
        self.requests.append(request)
        if self.error is not None and self.error_after is None:
            raise self.error
        chunks = [f"@{request.character_names[0]}: 응답 ", f"{len(self.requests)}번\n@나레이터: 끝"]
        for index, chunk in enumerate(chunks):
            if self.error is not None and index == self.error_after:
                raise self.error
            yield chunk


def model_id(db, name: str) -> int:
    return db.execute("SELECT id FROM models WHERE name = %s", (name,)).fetchone()["id"]


def start_session(client, story, intro, headers=PLAYER) -> int:
    response = client.post(
        f"{STORIES}/{story}/sessions",
        headers=headers,
        json={"intro_id": intro, "profile_kind": "preset", "profile_id": 1, "play_name": ""},
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


def change_model(client, session_id, model, headers=PLAYER):
    return client.put(f"{SESSIONS}/{session_id}/model", headers=headers, json={"model_id": model})


def turns(client, session_id, headers=PLAYER):
    response = client.get(f"{SESSIONS}/{session_id}/turns", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def generation_row(db, generation_id):
    return db.execute(
        """SELECT g.status, g.model_id, g.reported_model, g.error_code, g.text
           FROM conversation_generations g WHERE g.id = %s""",
        (generation_id,),
    ).fetchone()
