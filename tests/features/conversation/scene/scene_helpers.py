"""CHAT-US-08 장면 조회·직접 수정 테스트 도우미(저장 스토리·요청·가짜 공급자). 외부 호출 없음."""

import json

STORIES = "/api/v1/conversation/stories"
SESSIONS = "/api/v1/conversation/sessions"
GENERATIONS = "/api/v1/conversation/generations"
AUTHOR = {"X-User-Id": "1"}
PLAYER = {"X-User-Id": "2"}
STRANGER = {"X-User-Id": "3"}


def seed_story(db):
    """대화 시작이 가능한 공개 스토리(캐릭터 2명, 달력 날짜·시각·장소·착장). 반환: story, intro."""
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
    image = db.execute(
        """INSERT INTO images (owner_id, storage_key, mime_type, byte_size)
           VALUES (1, 'sample', 'image/png', 1) RETURNING id"""
    ).fetchone()["id"]
    db.execute(
        """INSERT INTO story_user_presets (story_id, name, image_id, description, position)
           VALUES (%s, '{{user}}', %s, '신입 탐정', 0)""",
        (story, image),
    )
    db.execute("INSERT INTO user_play_settings VALUES (1, '작가'), (2, '민서'), (3, '지나가는이')")
    return story, intro


class RecordingProvider:
    """모델 요청 메시지를 기록하고 정해진 응답 조각을 보낸다(외부 호출 없음)."""

    def __init__(self):
        self.requests = []

    def stream(self, request):
        self.requests.append(request)
        name = request.character_names[0]
        yield f"@{name}: 응답 "
        yield f"{len(self.requests)}번\n@나레이터: 끝"


class ScriptedJudge:
    """장면 판단 호출을 기록하고 정해진 판단 원문을 돌려준다."""

    def __init__(self):
        self.answers = []
        self.requests = []

    def stream(self, request):
        self.requests.append(request)
        yield self.answers.pop(0) if self.answers else '{"changed": false}'


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


def get_scene(client, session_id, headers=PLAYER):
    return client.get(f"{SESSIONS}/{session_id}/scene", headers=headers)


def put_scene(client, session_id, body, headers=PLAYER):
    # 짝 없는 서로게이트도 보낼 수 있게 ASCII 이스케이프 JSON으로 직접 보낸다(httpx json=은 UTF-8).
    # 버전을 따로 시험하지 않는 테스트는 세션 주인(PLAYER)이 지금 받은 버전을 채운다.
    if "version" not in body:
        current = get_scene(client, session_id)
        body = {**body, "version": current.json()["version"] if current.status_code == 200 else 0}
    return client.put(
        f"{SESSIONS}/{session_id}/scene",
        headers={**headers, "Content-Type": "application/json"},
        content=json.dumps(body),
    )


def editable(scene: dict) -> dict:
    """조회 응답에서 수정 요청 본문(고칠 수 있는 값 전체)을 만든다. 화면 폼이 하는 일과 같다."""
    body = {
        key: scene[key]
        for key in (
            "era",
            "year",
            "month",
            "day",
            "reference_event",
            "elapsed_day",
            "hour",
            "minute",
            "place",
        )
    }
    body["characters"] = [
        {"id": row["id"], "clothing": row["clothing"], "inner_thought": row["inner_thought"]}
        for row in scene["characters"]
    ]
    return body


def scene_snapshot(db, session_id):
    """거절 요청 전후로 같아야 하는 값: 장면 행·캐릭터 상태·변경 기록."""
    return (
        db.execute(
            "SELECT * FROM conversation_scene_states WHERE session_id = %s", (session_id,)
        ).fetchone(),
        db.execute(
            "SELECT * FROM conversation_character_states WHERE session_id = %s ORDER BY id",
            (session_id,),
        ).fetchall(),
        db.execute(
            "SELECT * FROM conversation_scene_changes WHERE session_id = %s ORDER BY id",
            (session_id,),
        ).fetchall(),
    )


def changes(db, session_id):
    return db.execute(
        "SELECT * FROM conversation_scene_changes WHERE session_id = %s ORDER BY id", (session_id,)
    ).fetchall()
