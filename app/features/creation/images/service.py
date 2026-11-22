"""CREATE-US-06 이미지 업로드·목록 저장. 형식·용량·대상별 규칙과 연결 정리의 경계."""

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import psycopg

from app.core.config import settings
from app.features.creation.common.errors import app_error
from app.features.creation.images import repository
from app.features.creation.images.schemas import (
    MAX_IMAGE_BYTES,
    CharacterImages,
    CoverImage,
    ImageItem,
    ImageTarget,
    StoryImagesResponse,
    StoryImagesSaveRequest,
)
from app.features.creation.images.storage import ImageStorage
from app.features.creation.save_visibility import working_copy
from app.features.creation.speech_style import service as speech_style

# 파일 앞부분(매직 넘버)으로 실제 형식을 판단한다. 파일 이름·Content-Type은 믿지 않는다.
_SIGNATURES = (
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"GIF87a", "image/gif", "gif"),
    (b"GIF89a", "image/gif", "gif"),
)


def detect_format(data: bytes) -> tuple[str, str] | None:
    for signature, mime_type, extension in _SIGNATURES:
        if data.startswith(signature):
            return mime_type, extension
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    return None


def file_url(storage_key: str) -> str:
    return f"{settings.api_prefix}/creation/image-files/{storage_key}"


def owned_image_urls(connection: psycopg.Connection, owner_id: int, image_ids) -> dict[int, str]:
    """이미지 id → 파일 URL. 소유자 이미지만 담고, 남의·없는 이미지는 빠진다(존재를 드러내지 않음).

    CREATE-US-05 프리셋 미리보기 복원용. 행을 잠그지 않아 읽기 전용 트랜잭션에서도 쓸 수 있다.
    """
    ids = sorted({i for i in image_ids if i is not None})
    rows = repository.read_owned_images(connection, owner_id, ids)
    return {row["id"]: file_url(row["storage_key"]) for row in rows}


def owned_image_items(
    connection: psycopg.Connection, owner_id: int, image_ids
) -> dict[int, ImageItem]:
    """이미지 id → 사진 API 항목. 소유자 이미지만 잠그지 않고 읽는다(남의·없는 이미지는 빠짐).

    CREATE-US-07-AC09 임시 저장한 새 캐릭터 사진 응답용. 읽기 전용 트랜잭션에서도 쓸 수 있다.
    """
    ids = sorted({i for i in image_ids if i is not None})
    return {
        row["id"]: to_item(row) for row in repository.read_owned_images(connection, owner_id, ids)
    }


def check_face_images(
    connection: psycopg.Connection, owner_id: int, image_ids: set[int], admitted: set[int]
) -> None:
    """캐릭터 얼굴로 쓸 이미지가 잠금 묶음 안의 소유자 이미지이고 GIF가 아닌지 확인한다.

    묶음 밖(남의 이미지·없는 ID·잠금 뒤 업로드)은 없는 이미지로 거절한다(잠금 순서 유지).
    """
    rows = {
        row["id"]: row
        for row in repository.read_owned_images(connection, owner_id, sorted(image_ids & admitted))
    }
    missing = sorted(image_ids - rows.keys())
    if missing:
        raise app_error(
            422, "IMAGE_NOT_FOUND", "찾을 수 없는 이미지가 있습니다.", image_ids=missing
        )
    if any(row["mime_type"] == "image/gif" for row in rows.values()):
        raise app_error(422, "IMAGE_GIF_NOT_ALLOWED", "GIF는 스토리 이미지에만 쓸 수 있습니다.")


def to_item(row) -> ImageItem:
    return ImageItem(
        image_id=row["id"],
        mime_type=row["mime_type"],
        byte_size=row["byte_size"],
        url=file_url(row["storage_key"]),
    )


def upload_image(
    connection: psycopg.Connection,
    storage: ImageStorage,
    owner_id: int,
    target: ImageTarget,
    data: bytes,
) -> ImageItem:
    """AC02·AC05: 5MB·형식·GIF 대상 검사 후 파일과 이미지 행을 만든다.

    대상 연결은 하지 않는다. 연결은 목록 저장(또는 프로필 저장)에서 한다.
    """
    if not data:
        raise app_error(422, "IMAGE_EMPTY", "빈 파일은 올릴 수 없습니다.")
    if len(data) > MAX_IMAGE_BYTES:
        raise app_error(
            422,
            "IMAGE_TOO_LARGE",
            "이미지는 파일 하나당 5MB까지 올릴 수 있습니다.",
            limit_bytes=MAX_IMAGE_BYTES,
        )
    detected = detect_format(data)
    if detected is None:
        raise app_error(
            422,
            "IMAGE_UNSUPPORTED_FORMAT",
            "JPG·PNG·WebP 이미지만 올릴 수 있습니다. GIF는 스토리 이미지에만 쓸 수 있습니다.",
        )
    mime_type, extension = detected
    if mime_type == "image/gif" and target != "story":
        raise app_error(422, "IMAGE_GIF_NOT_ALLOWED", "GIF는 스토리 이미지에만 쓸 수 있습니다.")
    storage_key = storage.save(data, extension)
    try:
        with connection.transaction():
            row = repository.insert_image(connection, owner_id, storage_key, mime_type, len(data))
    except Exception:
        # DB에 기록하지 못한 파일은 바로 지워 고아 파일을 남기지 않는다.
        storage.delete(storage_key)
        raise
    return to_item(row)


@dataclass(frozen=True)
class ImageFile:
    path: Path
    mime_type: str
    is_public: bool  # 공개 스토리 반영본에 연결돼 누구나 볼 수 있는지


def find_file(
    connection: psycopg.Connection,
    storage: ImageStorage,
    storage_key: str,
    viewer_id: int | None,
) -> ImageFile:
    """저장 키로 파일을 찾는다.

    공개 스토리의 반영본에 연결된 이미지는 누구나 볼 수 있다. 그 밖의 이미지(미등록·비공개
    스토리, 아직 연결하지 않은 업로드)는 소유자만 볼 수 있다. 권한이 없으면 이미지가
    있는지도 알리지 않도록 없는 이미지와 같은 404로 응답한다.
    """
    row = repository.find_file_image(connection, storage_key)
    allowed = row is not None and (row["is_public"] or row["owner_id"] == viewer_id)
    path = storage.path_of(storage_key) if allowed else None
    if path is None:
        raise app_error(404, "IMAGE_NOT_FOUND", "이미지를 찾을 수 없습니다.")
    return ImageFile(path=path, mime_type=row["mime_type"], is_public=row["is_public"])


def build_response(connection, story, request_id: str | None = None) -> StoryImagesResponse:
    """편집 화면용 목록. 이미지 임시 저장본이 있으면 그 목록을, 없으면 저장본을 쓴다."""
    story_id = story["id"]
    story_images = [to_item(r) for r in repository.read_story_images(connection, story_id)]
    by_character: dict[int, list[ImageItem]] = {}
    for row in repository.read_character_images(connection, story_id):
        by_character.setdefault(row["character_id"], []).append(to_item(row))
    copy = working_copy.read(connection, story_id)
    draft = copy["images"] if copy else None
    if draft is not None:
        # 임시 저장본에 담은 목록만 바꾸고, 담지 않은 캐릭터는 저장본 그대로 둔다(저장과 같은 규칙).
        ids = set(draft["story_image_ids"])
        for character in draft["characters"]:
            ids.update(character["image_ids"])
        items = {r["id"]: to_item(r) for r in repository.read_images(connection, sorted(ids))}
        story_images = [items[i] for i in draft["story_image_ids"] if i in items]
        for character in draft["characters"]:
            by_character[character["character_id"]] = [
                items[i] for i in character["image_ids"] if i in items
            ]
    characters = [
        CharacterImages(character_id=c["id"], name=c["name"], images=by_character.get(c["id"], []))
        for c in repository.list_characters(connection, story_id)
    ]
    cover = None
    if story_images:
        cover = CoverImage(source="story", image=story_images[0])
    elif characters and characters[0].images:
        cover = CoverImage(
            source="character",
            character_id=characters[0].character_id,
            image=characters[0].images[0],
        )
    return StoryImagesResponse(
        story_id=story_id,
        save_state=story["save_state"],
        saved_at=story["saved_at"],
        story_images=story_images,
        characters=characters,
        cover=cover,
        characters_missing_images=[c.character_id for c in characters if not c.images],
        request_id=request_id,
        working_copy_saved_at=copy["images_saved_at"] if draft is not None else None,
    )


CHARACTER_IMAGE_REQUIRED = "CHARACTER_IMAGE_REQUIRED"
CHARACTER_IMAGE_MESSAGE = "등장 캐릭터마다 이미지를 1장 이상 등록해 주세요."


def collect_image_issues(connection: psycopg.Connection, story_id: int) -> list[dict]:
    """AC08: 공개·비공개 저장을 막는 이미지 문제. 필드 이름은 기본 정보 캐릭터 순서를 따른다.

    공통 저장(save_visibility)도 이 함수로 같은 조건을 검사한다.
    스토리 이미지는 선택 입력이라 검사하지 않는다.
    """
    missing = set(repository.characters_without_images(connection, story_id))
    return [
        {
            "field": f"characters[{index}].images",
            "code": CHARACTER_IMAGE_REQUIRED,
            "message": CHARACTER_IMAGE_MESSAGE,
            "value": str(character["id"]),
        }
        for index, character in enumerate(repository.list_characters(connection, story_id))
        if character["id"] in missing
    ]


def get_story_images(connection: psycopg.Connection, story_id: int, owner_id: int):
    with connection.transaction():
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        story = speech_style.find_owned_story(connection, story_id, owner_id)
        return build_response(connection, story)


def save_story_images(
    connection: psycopg.Connection,
    story_id: int,
    owner_id: int,
    request_id: str,
    body: StoryImagesSaveRequest,
    storage: ImageStorage | None = None,
) -> StoryImagesResponse:
    """AC01·03·06·07·08: 스토리·캐릭터 이미지 목록과 순서를 한 트랜잭션에 저장한다.

    - 비공개·공개 스토리의 임시 저장: 임시 저장본에만 둔다. 이용자 화면은 그대로다.
    - 저장(또는 미등록 스토리): 바로 반영하고, 연결이 끊긴 예전 사진은 커밋 뒤 바로 지운다.
    """
    response, released = _save_story_images(connection, story_id, owner_id, request_id, body)
    if storage is not None and released:
        release_images(connection, storage, released)
    return response


AUTOSAVE_FINALIZE = "autosave_finalize"


def is_images_response(raw) -> bool:
    """기록한 응답이 사진 API(`StoryImagesResponse`) 모양인지(공통 저장 응답과 구분)."""
    return isinstance(raw, dict) and "story_images" in raw and "expression" not in raw


def photo_autosave_matches(story, copy, raw_response, auto_request_id: str) -> bool:
    """CREATE-US-07-AC09: 사진만 바꾼 자동 저장 기록이 지금도 그대로 확정 가능한지.

    스토리 잠금 안에서 부른다. 다음을 모두 만족할 때만 True다.
    - 기록한 응답이 사진 API 모양이고 확정 가능(자기 요청키)하며 같은 스토리·공개 상태다.
    - 응답의 저장본 시각(saved_at)·사진 임시 저장 시각이 지금과 같다.
    - 지금 사진 임시 저장본에 그 요청키 표시가 있고, 2~5번 공통 임시 저장본은 없다.
    """
    if not is_images_response(raw_response):
        return False
    if raw_response.get("finalize_auto_request_id") != auto_request_id or copy is None:
        return False
    recorded = StoryImagesResponse.model_validate(raw_response)
    marker = (copy["images"] or {}).get(AUTOSAVE_FINALIZE) or {}
    return (
        copy["creation"] is None
        and marker.get("request_id") == auto_request_id
        and recorded.story_id == story["id"]
        and recorded.save_state == story["save_state"]
        and recorded.saved_at == story["saved_at"]
        and recorded.working_copy_saved_at is not None
        and recorded.working_copy_saved_at == copy["images_saved_at"]
    )


def photo_autosave_unchanged(connection, story, owner_id: int, copy, auto_request_id: str) -> bool:
    """소유자의 요청 기록을 찾아 `photo_autosave_matches`로 확인한다."""
    previous = speech_style.find_write_request(connection, owner_id, auto_request_id)
    return bool(previous) and photo_autosave_matches(
        story, copy, previous["response"], auto_request_id
    )


def _photo_autosave_finalizable(connection, story, owner_id, copy) -> bool:
    """이번 사진 자동 저장을 확정 가능으로 표시할 수 있는지(쓰기 전 임시 저장본 기준).

    공통 임시 저장본이 없고, 사진 임시 저장본이 없거나 아직 바뀌지 않은 앞의 사진 자동 저장일
    때만이다. 그 밖의(관계없는) 사진 임시 저장본 위에는 이어 붙이지 않는다.
    """
    if not copy:
        return True
    if copy["creation"] is not None:
        return False
    if copy["images"] is None:
        return True
    marker = copy["images"].get(AUTOSAVE_FINALIZE)
    if not marker:
        return False
    return photo_autosave_unchanged(connection, story, owner_id, copy, marker["request_id"])


def request_hash_of(story_id: int, body: StoryImagesSaveRequest) -> str:
    """요청키 비교용 해시. mode를 생략·null로 보낸 요청은 mode를 빼고 해시해 기존과 같다."""
    exclude = {"mode"} if body.mode is None else set()
    payload = {
        "operation": "story-images-v2",
        "story_id": story_id,
        **body.model_dump(exclude=exclude),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def _save_story_images(connection, story_id, owner_id, request_id, body):
    request_hash = request_hash_of(story_id, body)
    with connection.transaction():
        speech_style.lock_write_request(connection, owner_id, request_id)
        story = speech_style.find_owned_story(connection, story_id, owner_id, lock=True)
        previous = speech_style.find_write_request(connection, owner_id, request_id)
        if previous:
            if previous["request_hash"] != request_hash:
                raise app_error(409, "IDEMPOTENCY_KEY_REUSED", "같은 요청키의 내용이 다릅니다.")
            return StoryImagesResponse.model_validate(previous["response"]), set()
        story_character_ids = {c["id"] for c in repository.list_characters(connection, story_id)}
        unknown = [
            c.character_id for c in body.characters if c.character_id not in story_character_ids
        ]
        if unknown:
            raise app_error(
                422,
                "CHARACTER_NOT_FOUND",
                "이 스토리에 없는 캐릭터입니다. 캐릭터를 먼저 저장해 주세요.",
                character_ids=unknown,
            )

        requested = set(body.story_image_ids)
        for character in body.characters:
            requested.update(character.image_ids)
        # 다른 사람의 이미지 ID를 넣은 요청은 존재하지 않는 이미지와 같게 거절한다.
        copy = working_copy.read(connection, story_id)
        old = working_copy.images_document_ids(copy["images"] if copy else None)
        linked = repository.linked_image_ids(
            connection, story_id, [character.character_id for character in body.characters]
        )
        images = {
            row["id"]: row
            for row in repository.find_owned_images(
                connection, owner_id, sorted(requested | old | linked)
            )
        }
        missing = sorted(requested - images.keys())
        if missing:
            raise app_error(
                422, "IMAGE_NOT_FOUND", "찾을 수 없는 이미지가 있습니다.", image_ids=missing
            )
        gif_on_character = [
            {"character_id": c.character_id, "image_id": i}
            for c in body.characters
            for i in c.image_ids
            if images[i]["mime_type"] == "image/gif"
        ]
        if gif_on_character:
            raise app_error(
                422,
                "IMAGE_GIF_NOT_ALLOWED",
                "GIF는 스토리 이미지에만 쓸 수 있습니다.",
                issues=gif_on_character,
            )

        released: set[int] = set()
        finalizable = False
        if body.kind == "temporary" and story["save_state"] != "draft":
            # 비공개·공개 스토리의 임시 저장: 필수 이미지 검사 없이 임시 저장본에만 둔다.
            document = body.model_dump(mode="json", exclude={"kind", "mode"})
            # CREATE-US-07-AC09: 쓰기 전 임시 저장본 기준으로 확정 가능 여부를 정하고,
            # 가능하면 내부 표시(응답에는 싣지 않음)를 남긴다.
            finalizable = body.mode == "auto" and _photo_autosave_finalizable(
                connection, story, owner_id, copy
            )
            if finalizable:
                document[AUTOSAVE_FINALIZE] = {"request_id": request_id}
            previous_copy = working_copy.write_images(connection, story_id, document, requested)
            repository.refresh_orphaned(connection, previous_copy | requested)
        else:
            before = _replace_lists(
                connection,
                story_id,
                body.story_image_ids,
                {c.character_id: c.image_ids for c in body.characters},
            )
            # AC08: 공개·비공개 스토리는 저장 뒤에도 캐릭터마다 이미지가 있어야 한다.
            # 조건을 깨면 예외로 트랜잭션 전체를 되돌린다(미등록 스토리는 그대로 저장).
            if story["save_state"] != "draft":
                issues = collect_image_issues(connection, story_id)
                if issues:
                    raise app_error(422, issues[0]["code"], issues[0]["message"], issues=issues)
            # 저장(반영)하면 이미지 임시 저장본은 더 필요 없다.
            previous_copy = working_copy.clear_images(connection, story_id)
            repository.refresh_orphaned(connection, before | requested | previous_copy)
            story["saved_at"] = repository.touch_saved_at(connection, story_id)
            released = (before | previous_copy) - requested

        response = build_response(connection, story, request_id)
        if finalizable:
            response.finalize_auto_request_id = request_id
        speech_style.remember_write_request(
            connection,
            owner_id,
            request_id,
            story_id,
            request_hash,
            response.model_dump(mode="json"),
        )
        return response, released


def _replace_lists(
    connection, story_id: int, story_image_ids: list[int], characters: dict[int, list[int]]
) -> set[int]:
    """스토리 이미지 목록과 담긴 캐릭터의 이미지 목록을 바꾼다.

    바꾸기 전에 연결돼 있던 이미지 ID를 돌려준다.
    """
    before = repository.linked_image_ids(connection, story_id, list(characters))
    repository.replace_story_images(connection, story_id, story_image_ids)
    for character_id, image_ids in characters.items():
        repository.replace_character_images(connection, character_id, image_ids)
    return before


def stage_combined_images(connection, story_ref, images, copy, admitted):
    """공통 임시 저장에서 보낸 사진 목록만 기존 편집본 위에 덮어쓴다."""
    if images is None:
        return
    previous = copy["images"] if copy else None
    story_id = story_ref.story_id
    people = {row["id"] for row in repository.list_characters(connection, story_id)}
    lists = {entry.character_id: entry.image_ids for entry in images.characters}
    if not lists.keys() <= people:
        raise app_error(422, "CHARACTER_NOT_FOUND", "이 스토리에 없는 캐릭터의 이미지입니다.")
    covers = images.story_image_ids
    if covers is None:
        covers = (
            previous["story_image_ids"]
            if previous
            else [row["id"] for row in repository.read_story_images(connection, story_id)]
        )
    merged = {
        row["character_id"]: row["image_ids"]
        for row in (previous["characters"] if previous else [])
        if row["character_id"] in people
    }
    merged.update(lists)
    document = {
        "story_image_ids": covers,
        "characters": [
            {"character_id": person, "image_ids": ids} for person, ids in merged.items()
        ],
    }
    requested = working_copy.images_document_ids(document)
    rows = {
        row["id"]: row
        for row in repository.read_owned_images(
            connection, story_ref.owner_id, sorted(requested & admitted)
        )
    }
    if requested - rows.keys():
        raise app_error(422, "IMAGE_NOT_FOUND", "찾을 수 없는 이미지가 있습니다.")
    if any(
        rows[image_id]["mime_type"] == "image/gif" for ids in merged.values() for image_id in ids
    ):
        raise app_error(422, "IMAGE_GIF_NOT_ALLOWED", "GIF는 스토리 이미지에만 쓸 수 있습니다.")
    detached = working_copy.write_images(connection, story_id, document, requested)
    repository.refresh_orphaned(connection, (detached | requested) & admitted)


def explicit_image_cohort(connection, story_id, character_ids):
    return repository.linked_image_ids(connection, story_id, character_ids)


def working_copy_image_cohort(connection: psycopg.Connection, story_id: int, copy) -> set[int]:
    """사진 임시본 반영에서 연결하거나 분리할 이미지 ID."""
    document = copy["images"] if copy else None
    if document is None:
        return set()
    ids = working_copy.images_document_ids(document)
    characters = [row["character_id"] for row in document["characters"]]
    return ids | repository.linked_image_ids(connection, story_id, characters)


def apply_working_copy(
    connection: psycopg.Connection, story_id: int, admitted: set[int]
) -> set[int]:
    """공통 「저장」에서 이미지 임시 저장본도 함께 반영한다(안 B: 저장하면 사진까지 한 번에).

    호출 측 트랜잭션 안에서 부르고, 반영한 뒤 호출 측이 완성 검사(캐릭터마다 이미지 1장 이상)를
    한다. 검사가 실패하면 트랜잭션 전체가 되돌아가 사진도 반영되지 않고 임시 저장본도 남는다.
    임시 저장 뒤 지워진 캐릭터의 목록은 건너뛴다.
    커밋 뒤 지울 후보(연결이 끊긴 이미지 ID)를 돌려준다.
    """
    copy = working_copy.read(connection, story_id)
    draft = copy["images"] if copy else None
    if draft is None:
        return set()
    existing = {c["id"] for c in repository.list_characters(connection, story_id)}
    characters = {
        c["character_id"]: c["image_ids"]
        for c in draft["characters"]
        if c["character_id"] in existing
    }
    requested = set(draft["story_image_ids"])
    for image_ids in characters.values():
        requested.update(image_ids)
    missing = sorted(requested - admitted)
    if missing:
        raise app_error(
            422, "IMAGE_NOT_FOUND", "찾을 수 없는 이미지가 있습니다.", image_ids=missing
        )
    before = _replace_lists(connection, story_id, draft["story_image_ids"], characters)
    previous_copy = working_copy.clear_images(connection, story_id)
    repository.refresh_orphaned(connection, (before | requested | previous_copy) & admitted)
    repository.touch_saved_at(connection, story_id)
    return ((before | previous_copy) - requested) & admitted


def apply_character_images(
    connection: psycopg.Connection,
    owner_id: int,
    lists: dict[int, list[int]],
    admitted: set[int],
    story_id: int | None = None,
    story_image_ids: list[int] | None = None,
) -> set[int]:
    """CREATE-US-07-AC07: 공통 「저장」이 보낸 캐릭터별 얼굴·스토리 표지 이미지 목록을 반영한다.

    `lists`는 이 스토리의 캐릭터 id → 이미지 ID 목록(순서가 position)이며, 호출 측이 캐릭터를
    확인·생성한 뒤 같은 트랜잭션에서 부른다. 사진 임시 저장본 반영 뒤에 부르면 보낸 캐릭터만
    덮어쓰고, 담지 않은 캐릭터·스토리 이미지는 그대로 둔다.
    `admitted`는 이 트랜잭션이 처음에 id 순으로 잠근 묶음이다.
    소유·존재 확인과 연결은 그 안에서만 한다. 묶음 밖의 ID(남의 이미지·없는 ID·
    잠금 뒤 업로드)는 없는 이미지로 거절해 잠금 순서를 지킨다.
    거절은 예외라 호출 측 트랜잭션 전체가 되돌아간다.
    `story_image_ids`(순서가 position)가 있으면 `story_id`의 스토리 이미지 목록 전체를 같은
    규칙(소유·존재 확인, 묶음 안에서만 연결)으로 바꾼다. 스토리 이미지는 GIF를 허용한다.
    None이면 스토리 이미지는 그대로 둔다.
    커밋 뒤 지울 후보(연결이 끊긴 예전 이미지 ID)를 돌려준다.
    """
    requested = {image_id for image_ids in lists.values() for image_id in image_ids}
    if story_image_ids is not None:
        requested |= set(story_image_ids)
    images = {
        row["id"]: row
        for row in repository.read_owned_images(connection, owner_id, sorted(requested & admitted))
    }
    missing = sorted(requested - images.keys())
    if missing:
        raise app_error(
            422, "IMAGE_NOT_FOUND", "찾을 수 없는 이미지가 있습니다.", image_ids=missing
        )
    gif_on_character = [
        {"character_id": character_id, "image_id": image_id}
        for character_id, image_ids in lists.items()
        for image_id in image_ids
        if images[image_id]["mime_type"] == "image/gif"
    ]
    if gif_on_character:
        raise app_error(
            422,
            "IMAGE_GIF_NOT_ALLOWED",
            "GIF는 스토리 이미지에만 쓸 수 있습니다.",
            issues=gif_on_character,
        )
    before = character_image_ids(connection, set(lists))
    if story_image_ids is not None:
        before |= repository.linked_image_ids(connection, story_id, [])
        repository.replace_story_images(connection, story_id, story_image_ids)
    for character_id, image_ids in lists.items():
        repository.replace_character_images(connection, character_id, image_ids)
    repository.refresh_orphaned(connection, (before | requested) & admitted)
    return (before - requested) & admitted


def character_image_ids(connection: psycopg.Connection, character_ids: set[int]) -> set[int]:
    """캐릭터들에 연결된 이미지 ID. 캐릭터를 지우기(CASCADE) 전에 읽어 정리 후보로 쓴다."""
    if not character_ids:
        return set()
    return repository.character_image_ids(connection, sorted(character_ids))


def images_working_copy_saved_at(connection: psycopg.Connection, story_id: int):
    """이미지 임시 저장본 시각. 없으면 None.

    공통 저장 화면이 「저장」할 사진이 있는지 알 때 쓴다.
    """
    copy = working_copy.read(connection, story_id)
    return copy["images_saved_at"] if copy and copy["images"] is not None else None


logger = logging.getLogger(__name__)


@dataclass
class CleanupResult:
    marked: int = 0  # 이번에 미사용으로 표시한 이미지 수
    unmarked: int = 0  # 다시 연결돼 표시를 지운 이미지 수
    deleted: list[int] = field(default_factory=list)
    kept: list[int] = field(
        default_factory=list
    )  # 삭제 직전 재확인에서 사용 중이거나 잠겨 있던 이미지
    failed: list[int] = field(
        default_factory=list
    )  # 파일 삭제 실패. 행을 남겨 다음 실행에서 다시 시도


def cleanup_unused_images(
    connection: psycopg.Connection, storage: ImageStorage, grace: timedelta
) -> CleanupResult:
    """AC11: 유예 기간이 지난 미사용 이미지만 파일과 행을 지운다.

    1. 연결 상태와 미사용 표시를 맞춘다(새로 끊긴 것은 지금부터 유예 시작, 다시 연결된 것은 해제).
       다른 저장이 잠근 행은 기다리지 않고 건너뛰어 다음 실행에서 다시 맞춘다.
    2. 유예가 지난 이미지마다 별도 트랜잭션에서 행을 잠그고 연결을 다시 확인한다.
       다른 저장이 연결하는 중이면(외래 키 잠금) 기다리지 않고 건너뛰어 그 저장을 깨뜨리지 않는다.
    3. 파일을 먼저 지우고 성공했을 때만 행을 지운다. 파일 삭제가 실패하면 행을 남겨 다시 시도한다.
       잠근 행은 지울 때까지 새 연결이 생길 수 없다(외래 키 검사가 잠금을 기다림).
    """
    seconds = grace.total_seconds()
    result = CleanupResult()
    with connection.transaction():
        result.marked = repository.mark_unused_images(connection)
        result.unmarked = repository.unmark_used_images(connection)
    for image_id in repository.expired_image_ids(connection, seconds):
        _delete_if_unused(connection, storage, image_id, seconds, result)
    return result


def refresh_orphaned_images(connection: psycopg.Connection, image_ids: set[int]) -> None:
    """다른 기능이 이미지 연결을 바꾼 뒤, 그 이미지들의 미사용 시작 시각을 다시 계산한다(AC11).

    연결이 하나라도 남으면 NULL, 없으면 미사용 시각을 기록한다. 호출자의 트랜잭션 안에서,
    연결 쓰기를 마친 뒤 부른다. 커밋·롤백하지 않는다.
    """
    repository.refresh_orphaned(connection, image_ids)


def release_images(
    connection: psycopg.Connection, storage: ImageStorage, image_ids: set[int]
) -> CleanupResult:
    """저장(반영)으로 연결이 끊긴 예전 사진을 바로 지운다(2026-11-20 결정: 보관하지 않음).

    정리 작업과 같은 방식(행 잠금 → 사용 재확인 → 파일 삭제 → 행 삭제)이라, 다른 대상·임시
    저장본이 함께 쓰는 사진은 남는다. 실패하면 행이 남아 정리 작업이 다시 시도한다.
    저장 트랜잭션이 끝난 뒤에 부른다.
    """
    result = CleanupResult()
    for image_id in sorted(image_ids):
        _delete_if_unused(connection, storage, image_id, 0, result)
    return result


def _delete_if_unused(connection, storage, image_id: int, seconds: float, result: CleanupResult):
    try:
        with connection.transaction():
            row = repository.lock_expired_image(connection, image_id, seconds)
            if row is None:
                result.kept.append(image_id)
                return
            if repository.is_image_used(connection, image_id):
                repository.clear_orphaned(connection, image_id)
                result.kept.append(image_id)
                return
            storage.delete(row["storage_key"])
            repository.delete_image_row(connection, image_id)
            result.deleted.append(image_id)
    except Exception:
        logger.exception("이미지 정리 실패(다음 실행에서 다시 시도): image_id=%s", image_id)
        result.failed.append(image_id)
