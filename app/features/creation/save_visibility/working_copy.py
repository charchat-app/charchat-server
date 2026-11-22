"""임시 저장본(스토리마다 1개). 비공개·공개 스토리를 고치다 임시 저장한 내용을 둔다.

공개 상태(미등록·비공개·공개)와 저장 상태(임시 저장·저장)를 나눈 결정(2026-11-20)에 따른다.
- 임시 저장: 이 표에만 쓴다. 이용자가 보는 본 테이블은 그대로다.
- 저장: 본 테이블에 반영하고 그 영역의 임시 저장본을 비운다.
미등록 스토리는 아무도 보지 않으므로 임시 저장도 본 테이블에 바로 쓰고 이 표를 쓰지 않는다.

`story_working_copy_images`는 임시 저장본 행 전체가 쓰는 이미지다(파일 정리 보호용 사용 연결).
6번 이미지 문서(`images`)의 이미지와 공통 문서(`creation`)의 임시 프리셋 이미지·새 캐릭터
사진(CREATE-US-07-AC09)을 합친다. 한쪽 문서를 쓰거나 비울 때 두 문서를 기준으로 맞춘다.
다른 쪽 문서의 연결은 유지한다.
"""

from collections.abc import Collection

import psycopg
from psycopg.types.json import Jsonb


def read(connection: psycopg.Connection, story_id: int):
    return connection.execute(
        """SELECT creation, creation_saved_at, images, images_saved_at
           FROM story_working_copies WHERE story_id = %s""",
        (story_id,),
    ).fetchone()


def images_document_ids(document: dict | None) -> set[int]:
    """6번 이미지 문서가 쓰는 이미지 ID. 쓸 때 이미 소유·존재를 확인한 값이다."""
    if not document:
        return set()
    ids = set(document["story_image_ids"])
    for character in document["characters"]:
        ids.update(character["image_ids"])
    return ids


def staged_presets(document: dict | None) -> list[dict] | None:
    """2~5번 공통 문서에 임시 저장한 프리셋 원문. 임시 저장하지 않았으면 None, 비움은 []."""
    return document.get("presets") if document else None


def preset_image_ids(document: dict | None) -> set[int]:
    """2~5번 공통 문서의 임시 프리셋이 가리키는 이미지 ID(검사하지 않은 원문)."""
    return {p["image_id"] for p in staged_presets(document) or () if p.get("image_id") is not None}


STAGED_CHARACTER_IMAGES = "staged_character_images"


def staged_character_images(document: dict | None) -> list[dict]:
    """CREATE-US-07-AC09: 공통 문서의 임시 `basic`과 짝지은 새 캐릭터 사진.

    [{basic_index, image_ids}] (basic_index 순). 쓸 때 소유·GIF를 확인했다. 없으면 [].
    """
    return list((document or {}).get(STAGED_CHARACTER_IMAGES) or ())


def staged_character_image_ids(document: dict | None) -> set[int]:
    return {i for entry in staged_character_images(document) for i in entry["image_ids"]}


def creation_image_ids(document: dict | None) -> set[int]:
    """공통 문서가 가리키는 이미지 ID(임시 프리셋 + 새 캐릭터 사진)."""
    return preset_image_ids(document) | staged_character_image_ids(document)


def write_creation(
    connection: psycopg.Connection,
    story_id: int,
    document: dict,
    *,
    admitted: Collection[int] = (),
) -> tuple[object, set[int]]:
    """2~5번 공통 저장 문서를 임시 저장본에 둔다.

    (임시 저장 시각, 연결이 생기거나 끊긴 이미지 ID)를 돌려준다. 호출자는 그 이미지의
    미사용 표시를 다시 계산한다. `admitted`는 `_sync_image_links`를 본다.
    """
    saved_at = connection.execute(
        """INSERT INTO story_working_copies(story_id, creation, creation_saved_at)
           VALUES (%s, %s, clock_timestamp())
           ON CONFLICT (story_id) DO UPDATE
             SET creation = EXCLUDED.creation, creation_saved_at = EXCLUDED.creation_saved_at
           RETURNING creation_saved_at""",
        (story_id, Jsonb(document)),
    ).fetchone()["creation_saved_at"]
    return saved_at, _sync_image_links(connection, story_id, admitted)


def write_images(
    connection: psycopg.Connection, story_id: int, document: dict, image_ids: set[int]
) -> set[int]:
    """이미지 목록 문서를 임시 저장본에 둔다. 이전 이미지 문서가 쓰던 이미지 ID를 돌려준다.

    `image_ids`는 호출자가 확인한 `document`의 이미지다. 연결은 두 문서 기준으로 다시 맞추며,
    임시 프리셋의 기존 연결은 그대로 둔다.
    """
    previous = read(connection, story_id)
    connection.execute(
        """INSERT INTO story_working_copies(story_id, images, images_saved_at)
           VALUES (%s, %s, clock_timestamp())
           ON CONFLICT (story_id) DO UPDATE
             SET images = EXCLUDED.images, images_saved_at = EXCLUDED.images_saved_at""",
        (story_id, Jsonb(document)),
    )
    _sync_image_links(connection, story_id, ())
    return images_document_ids(previous["images"] if previous else None)


def clear_creation(
    connection: psycopg.Connection, story_id: int, *, admitted: Collection[int] = ()
) -> set[int]:
    """2~5번 임시 저장본을 비운다. 연결이 끊긴 임시 프리셋 이미지 ID를 돌려준다.

    6번 이미지 문서와 그 연결은 그대로 둔다.
    """
    connection.execute(
        """UPDATE story_working_copies SET creation = NULL, creation_saved_at = NULL
           WHERE story_id = %s""",
        (story_id,),
    )
    changed = _sync_image_links(connection, story_id, admitted)
    _drop_if_empty(connection, story_id)
    return changed


def clear_images(connection: psycopg.Connection, story_id: int) -> set[int]:
    """이미지 임시 저장본을 비운다. 그 이미지 문서가 쓰던 이미지 ID를 돌려준다.

    2~5번 공통 문서(장면·임시 프리셋)와 임시 프리셋 이미지 연결은 그대로 둔다.
    """
    previous = read(connection, story_id)
    connection.execute(
        """UPDATE story_working_copies SET images = NULL, images_saved_at = NULL
           WHERE story_id = %s""",
        (story_id,),
    )
    _sync_image_links(connection, story_id, ())
    _drop_if_empty(connection, story_id)
    return images_document_ids(previous["images"] if previous else None)


def _sync_image_links(connection, story_id: int, admitted: Collection[int]) -> set[int]:
    """연결을 두 문서 기준으로 맞추고, 새로 생기거나 끊긴 이미지 ID를 돌려준다.

    공통 문서의 이미지(임시 프리셋 원문·새 캐릭터 사진, CREATE-US-07-AC09)는 같은 규칙을 따른다.
    임시 프리셋 이미지는 검사 전 원문이다. 이미 연결된 ID는 문서에 남아 있는 동안 유지하고,
    새 ID는 `admitted`(호출자가 이 트랜잭션에서 id 순으로 잠근, 스토리 제작자의 이미지)
    안의 것만 연결한다. 그래서 남의·없는 이미지가 보호되거나 외래 키 오류가 나지 않고,
    잠금 순서 밖에서 이미지 행을 새로 잠그지 않는다. 원문은 문서에 그대로 남는다.
    """
    row = read(connection, story_id)
    previous = {
        r["image_id"]
        for r in connection.execute(
            "SELECT image_id FROM story_working_copy_images WHERE story_id = %s", (story_id,)
        ).fetchall()
    }
    wanted: set[int] = set()
    if row:
        wanted = images_document_ids(row["images"])
        wanted |= creation_image_ids(row["creation"]) & (previous | set(admitted))
    removed, added = previous - wanted, wanted - previous
    if removed:
        connection.execute(
            """DELETE FROM story_working_copy_images
               WHERE story_id = %s AND image_id = ANY(%s::bigint[])""",
            (story_id, sorted(removed)),
        )
    if added:
        connection.execute(
            """INSERT INTO story_working_copy_images(story_id, image_id)
               SELECT %s, unnest(%s::bigint[])""",
            (story_id, sorted(added)),
        )
    return removed | added


def _drop_if_empty(connection, story_id: int) -> None:
    connection.execute(
        """DELETE FROM story_working_copies
           WHERE story_id = %s AND creation IS NULL AND images IS NULL""",
        (story_id,),
    )
