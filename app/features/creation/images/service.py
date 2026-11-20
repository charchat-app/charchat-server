"""CREATE-US-06 이미지 업로드·목록 저장. 형식·용량·대상별 규칙과 연결 정리의 경계."""

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import timedelta

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


def find_file(connection: psycopg.Connection, storage: ImageStorage, storage_key: str):
    """저장 키로 파일을 찾는다. 키는 추측할 수 없는 무작위 값이라 키를 아는 요청만 열람한다."""
    row = repository.find_file_image(connection, storage_key)
    path = storage.path_of(storage_key) if row else None
    if path is None:
        raise app_error(404, "IMAGE_NOT_FOUND", "이미지를 찾을 수 없습니다.")
    return path, row["mime_type"]


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


def _save_story_images(connection, story_id, owner_id, request_id, body):
    payload = {"operation": "story-images-v2", "story_id": story_id, **body.model_dump()}
    request_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
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
        images = {
            row["id"]: row
            for row in repository.find_owned_images(connection, owner_id, sorted(requested))
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
        if body.kind == "temporary" and story["save_state"] != "draft":
            # 비공개·공개 스토리의 임시 저장: 필수 이미지 검사 없이 임시 저장본에만 둔다.
            document = body.model_dump(mode="json", exclude={"kind"})
            previous_copy = working_copy.write_images(connection, story_id, document, requested)
            repository.refresh_orphaned(connection, previous_copy | requested)
        else:
            touched_characters = [c.character_id for c in body.characters]
            before = repository.linked_image_ids(connection, story_id, touched_characters)
            repository.replace_story_images(connection, story_id, body.story_image_ids)
            for character in body.characters:
                repository.replace_character_images(
                    connection, character.character_id, character.image_ids
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
        speech_style.remember_write_request(
            connection,
            owner_id,
            request_id,
            story_id,
            request_hash,
            response.model_dump(mode="json"),
        )
        return response, released


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
