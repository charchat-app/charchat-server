"""CREATE-US-05 프리셋 이미지 미리보기 복원 (실제 HTTP·PostgreSQL).

대상: CREATE-US-05 서버 통합 조각 — /creation 응답 프리셋의 읽기 전용 `image_url`.
- 저장본·임시 저장본 프리셋 모두 새로 조회(GET)할 때 소유자 이미지의 파일 URL을 채운다.
- 남의 이미지·없는 이미지·이미지 없음은 null이다(남의 이미지 URL을 노출하지 않는다).
- URL은 임시 저장본 JSON에 저장하지 않는다. 요청키 재전송은 기록된 응답을 그대로 돌려준다.
- 소유자가 아니면 스토리 자체를 찾을 수 없다(404).

`TEST_DATABASE_URL`(폐기 가능한 전용 테스트 DB)이 없으면 건너뛴다. 건너뛴 결과는 검증 완료가 아니다.
"""

import secrets

import psycopg

from tests.features.creation.speech_style.conftest import OWNER_ID
from tests.features.creation.speech_style.test_common_save_api import current_payload, put, url
from tests.features.creation.speech_style.test_working_copy_save import publish

OTHER_ID = 200
FILES = "/api/v1/creation/image-files/"


def image(dsn, owner_id=OWNER_ID):
    """업로드만 된 PNG 이미지 행. (id, storage_key)"""
    key = f"{secrets.token_hex(16)}.png"
    with psycopg.connect(dsn) as connection:
        image_id = connection.execute(
            """INSERT INTO images(owner_id, storage_key, mime_type, byte_size, orphaned_at)
               VALUES (%s, %s, 'image/png', 1, clock_timestamp()) RETURNING id""",
            (owner_id, key),
        ).fetchone()[0]
    return image_id, key


def insert_preset(dsn, story, name, image_id, position):
    with psycopg.connect(dsn) as connection:
        return connection.execute(
            """INSERT INTO story_user_presets(story_id, name, image_id, position)
               VALUES (%s, %s, %s, %s) RETURNING id""",
            (story["id"], name, image_id, position),
        ).fetchone()[0]


def staged_json(dsn, story):
    with psycopg.connect(dsn) as connection:
        return connection.execute(
            "SELECT creation FROM story_working_copies WHERE story_id=%s", (story["id"],)
        ).fetchone()[0]


def previews(presets):
    return [(p["name"], p["image_id"], p["image_url"]) for p in presets]


def test_fresh_get_restores_canonical_preset_image_url_CREATE_US_05(context):
    client, _, story, _, dsn = context
    publish(client, story, dsn)
    owned, owned_key = image(dsn)
    foreign, _ = image(dsn, owner_id=OTHER_ID)
    insert_preset(dsn, story, "내 사진", owned, 0)
    insert_preset(dsn, story, "남의 사진", foreign, 1)
    insert_preset(dsn, story, "사진 없음", None, 2)

    result = client.get(url(story))

    assert result.status_code == 200, result.text
    data = result.json()
    assert data["presets_pending"] is False
    assert previews(data["presets"]) == [
        ("내 사진", owned, FILES + owned_key),
        ("남의 사진", foreign, None),
        ("사진 없음", None, None),
    ]
    # 다른 사용자는 스토리를 찾을 수 없어 미리보기 URL도 받지 못한다.
    stranger = client.get(url(story), headers={"X-User-Id": str(OTHER_ID)})
    assert stranger.status_code == 404


def test_fresh_get_restores_pending_preset_image_url_CREATE_US_05(context):
    client, _, story, _, dsn = context
    saved = publish(client, story, dsn)
    owned, owned_key = image(dsn)
    foreign, _ = image(dsn, owner_id=OTHER_ID)
    raw = [
        {"name": "내 사진", "image_id": owned},
        {"name": "남의 사진", "image_id": foreign},
        {"name": "없는 사진", "image_id": 987654321},
        {"name": "사진 없음", "image_id": None},
    ]
    payload = {**current_payload(saved), "kind": "temporary", "presets": raw}
    expected = [
        ("내 사진", owned, FILES + owned_key),
        ("남의 사진", foreign, None),
        ("없는 사진", 987654321, None),
        ("사진 없음", None, None),
    ]

    staged = put(client, story, payload, "preview-temp")
    assert staged.status_code == 200, staged.text
    assert previews(staged.json()["presets"]) == expected

    fresh = client.get(url(story))
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["presets_pending"] is True
    assert previews(fresh.json()["presets"]) == expected
    # URL은 응답 전용이다. 임시 저장본 원문에는 담지 않는다.
    assert all("image_url" not in item for item in staged_json(dsn, story)["presets"])

    # 같은 요청키 재전송은 기록된 응답(미리보기 포함)을 그대로 돌려준다.
    replay = put(client, story, payload, "preview-temp")
    assert replay.status_code == 200, replay.text
    assert replay.json() == staged.json()
