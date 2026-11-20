"""CREATE-US-06 테스트 공용 값과 요청 도우미."""

import secrets

from psycopg.rows import tuple_row

OWNER_ID = 100
OTHER_ID = 200

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 32
GIF = b"GIF89a" + b"\x01\x00\x01\x00" + b"\x00" * 32


def create_story(db, owner_id=OWNER_ID, characters=("하린", "도윤"), save_state="draft"):
    story = db.execute(
        "INSERT INTO stories(owner_id, title) VALUES (%s, %s) RETURNING id",
        (owner_id, "가상 스토리"),
    ).fetchone()
    ids = []
    for position, name in enumerate(characters):
        row = db.execute(
            """INSERT INTO characters(story_id, name, position) VALUES (%s, %s, %s)
               RETURNING id""",
            (story["id"], name, position),
        ).fetchone()
        ids.append(row["id"])
    if save_state != "draft":
        db.execute("UPDATE stories SET save_state=%s WHERE id=%s", (save_state, story["id"]))
    return story["id"], ids


def give_face_images(connection, story_id):
    """완성 저장 테스트 준비: 이미지가 없는 등장 캐릭터마다 이미지 1장을 연결한다.

    공개·비공개 저장은 캐릭터마다 이미지가 필요하므로(CREATE-US-06-AC08), 다른 스토리의
    완성 저장 테스트가 이미지와 무관한 조건만 검사하도록 미리 채워 둔다.
    """
    cursor = connection.cursor(row_factory=tuple_row)
    owner_id = cursor.execute("SELECT owner_id FROM stories WHERE id=%s", (story_id,)).fetchone()[0]
    characters = cursor.execute(
        """SELECT c.id FROM characters c WHERE c.story_id=%s
           AND NOT EXISTS (SELECT 1 FROM character_images ci WHERE ci.character_id=c.id)""",
        (story_id,),
    ).fetchall()
    for (character_id,) in characters:
        image_id = cursor.execute(
            """INSERT INTO images(owner_id, storage_key, mime_type, byte_size)
               VALUES (%s, %s, 'image/png', 1) RETURNING id""",
            (owner_id, f"{secrets.token_hex(16)}.png"),
        ).fetchone()[0]
        cursor.execute(
            "INSERT INTO character_images(character_id, image_id, position) VALUES (%s, %s, 0)",
            (character_id, image_id),
        )


def upload(api, data=PNG, target="character", name="a.png", headers=None):
    return api.post(
        "/api/v1/creation/images",
        params={"target": target},
        files={"file": (name, data, "application/octet-stream")},
        headers=headers or {},
    )


def uploaded_id(api, data=PNG, target="character", headers=None):
    response = upload(api, data, target, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["image_id"]
