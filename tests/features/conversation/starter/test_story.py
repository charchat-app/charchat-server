"""CHAT-US-01 저장된 스토리 선택과 접근 권한."""

import pytest

STORY = "/api/v1/conversation/stories"
PLAYER = {"X-User-Id": "2"}


def test_story_contract_reads_saved_details_only(client, db):
    story = db.execute(
        """INSERT INTO stories (owner_id, title, subtitle, world_description, save_state)
           VALUES (1, 'Title', 'Subtitle', 'World', 'public') RETURNING id"""
    ).fetchone()["id"]
    char = db.execute(
        """INSERT INTO characters (story_id, name, description, position)
           VALUES (%s, 'Aria', 'Guard', 0) RETURNING id""",
        (story,),
    ).fetchone()["id"]
    late = db.execute(
        "INSERT INTO intros (story_id, title, position) VALUES (%s, 'Late', 7) RETURNING id",
        (story,),
    ).fetchone()["id"]
    first = db.execute(
        "INSERT INTO intros (story_id, title, position) VALUES (%s, 'First', 3) RETURNING id",
        (story,),
    ).fetchone()["id"]
    db.execute(
        """INSERT INTO intro_messages (intro_id, speaker_kind, character_id, body, position)
           VALUES (%s, 'narrator', NULL, 'First!', 4), (%s, 'character', %s, 'Hi {{user}}', 2)""",
        (first, first, char),
    )
    image = db.execute(
        """INSERT INTO images (owner_id, storage_key, mime_type, byte_size)
           VALUES (1, 'test', 'image/png', 1) RETURNING id"""
    ).fetchone()["id"]
    db.execute(
        """INSERT INTO story_user_presets (story_id, name, image_id, description, position)
           VALUES (%s, '{{user}}', %s, 'Preset info', 0), (%s, 'Incomplete', NULL, NULL, 1)""",
        (story, image, story),
    )
    db.execute(
        """INSERT INTO user_profiles (owner_id, name, image_id)
           VALUES (1, 'Not mine', %s), (2, 'Mine', %s)""",
        (image, image),
    )
    db.execute("INSERT INTO user_play_settings VALUES (2, 'Default')")
    db.execute(
        """INSERT INTO story_working_copies (story_id, creation, creation_saved_at)
           VALUES (%s, '{"title":"Unpublished"}', now())""",
        (story,),
    )
    db.execute(
        "INSERT INTO intro_character_states VALUES (%s, %s, 'Coat', 'Hidden thought')",
        (first, char),
    )
    response = client.get(f"{STORY}/{story}", headers=PLAYER)
    assert response.status_code == 200
    assert response.json() == {
        "id": story,
        "title": "Title",
        "subtitle": "Subtitle",
        "world_description": "World",
        "effective_rating": None,
        "characters": [{"id": char, "name": "Aria", "description": "Guard"}],
        "intros": [
            {
                "id": first,
                "title": "First",
                "messages": [
                    {"speaker_kind": "character", "speaker_name": "Aria", "body": "Hi {{user}}"},
                    {"speaker_kind": "narrator", "speaker_name": None, "body": "First!"},
                ],
            },
            {"id": late, "title": "Late", "messages": []},
        ],
        "profiles": [
            {
                "id": 1,
                "kind": "preset",
                "name": "{{user}}",
                "description": "Preset info",
                "initial_name": "Default",
            },
            {
                "id": 2,
                "kind": "personal",
                "name": "Mine",
                "description": "",
                "initial_name": "Mine",
            },
        ],
        "existing_session_id": None,
        "tags": [],
    }


@pytest.mark.parametrize(
    ("state", "owner", "deleted", "status"),
    [
        ("private", 2, False, 200),
        ("private", 1, False, 404),
        ("draft", 2, False, 404),
        ("public", 2, True, 404),
    ],
)
def test_story_access(client, db, state, owner, deleted, status):
    story = db.execute(
        """INSERT INTO stories (owner_id, save_state, deleted_at)
           VALUES (%s, %s, CASE WHEN %s THEN now() END) RETURNING id""",
        (owner, state, deleted),
    ).fetchone()["id"]
    assert client.get(f"{STORY}/{story}", headers=PLAYER).status_code == status
