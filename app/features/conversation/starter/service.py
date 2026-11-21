"""CHAT-US-01 저장본만 조회하고 대화 시작 규칙을 적용한다."""

import json
from collections import defaultdict
from hashlib import sha256

from psycopg import Connection

from app.core.exceptions import AppError
from app.features.conversation.starter import repository
from app.features.conversation.starter.schemas import (
    CharacterResponse,
    IntroResponse,
    MessageResponse,
    ProfileResponse,
    SessionCreatedResponse,
    SessionCreateRequest,
    SessionResponse,
    StoryCharacterResponse,
    StoryResponse,
)
from app.features.creation.common.names import has_surrounding_space, name_compare_key

USER_PLACEHOLDER = "{{user}}"
PROFILE_TEXT_LIMIT = 1000


class StoryNotFoundError(AppError):
    status = 404
    code = "CONVERSATION_STORY_NOT_FOUND"
    title = "대화를 시작할 수 있는 스토리를 찾을 수 없습니다"


def _real_name(name: str) -> bool:
    return bool(name.strip()) and name != USER_PLACEHOLDER and not has_surrounding_space(name)


def _is_placeholder_preset(profile) -> bool:
    return profile["kind"] == "preset" and profile["name"] == USER_PLACEHOLDER


def _eligible_profile(profile) -> bool:
    name = profile["name"]
    return bool(
        profile["image_id"] is not None
        and name
        and (_is_placeholder_preset(profile) or _real_name(name))
        and len(name) + len(profile["description"] or "") <= PROFILE_TEXT_LIMIT
    )


def _message(row) -> MessageResponse:
    return MessageResponse(
        speaker_kind=row["speaker_kind"], speaker_name=row["speaker_name"], body=row["body"] or ""
    )


def get_story(conn: Connection, owner_id: int, story_id: int) -> StoryResponse:
    with conn.transaction():
        repository.set_consistent_read(conn)
        story = repository.get_story(conn, story_id, owner_id)
        if story is None:
            raise StoryNotFoundError()
        messages = defaultdict(list)
        for row in repository.list_intro_messages(conn, story_id):
            messages[row["intro_id"]].append(_message(row))
        default_name = repository.get_default_play_name(conn, owner_id)
        return StoryResponse(
            id=story_id,
            title=story["title"] or "",
            subtitle=story["subtitle"] or "",
            world_description=story["world_description"] or "",
            # dev 초기화 스키마에 관람 등급 컬럼이 아직 없다.
            effective_rating=None,
            characters=[
                StoryCharacterResponse(
                    id=row["id"], name=row["name"] or "", description=row["description"] or ""
                )
                for row in repository.list_characters(conn, story_id)
            ],
            intros=[
                IntroResponse(id=row["id"], title=row["title"] or "", messages=messages[row["id"]])
                for row in repository.list_intros(conn, story_id)
            ],
            profiles=[
                ProfileResponse(
                    id=row["id"],
                    kind=row["kind"],
                    name=row["name"],
                    description=row["description"] or "",
                    initial_name=(default_name or None)
                    if _is_placeholder_preset(row)
                    else row["name"],
                )
                for row in repository.list_profiles(conn, story_id, owner_id)
                if _eligible_profile(row)
            ],
            existing_session_id=repository.get_latest_session_id(conn, story_id, owner_id),
            tags=repository.list_tags(conn, story_id),
        )


class IntroNotFoundError(AppError):
    status = 422
    code = "CONVERSATION_INTRO_NOT_FOUND"
    title = "이 스토리의 인트로를 선택해 주세요"


class ProfileNotFoundError(AppError):
    status = 422
    code = "CONVERSATION_PROFILE_NOT_FOUND"
    title = "사용 가능한 프로필을 선택해 주세요"


class InvalidPlayNameError(AppError):
    status = 422
    code = "CONVERSATION_INVALID_PLAY_NAME"
    title = "사용 가능한 플레이 이름을 입력해 주세요"


class SessionNotFoundError(AppError):
    status = 404
    code = "CONVERSATION_SESSION_NOT_FOUND"
    title = "접근할 수 있는 대화 세션을 찾을 수 없습니다"


def _play_name(conn: Connection, owner_id: int, request: SessionCreateRequest, profile, chars):
    name = request.play_name
    if name == "" and _is_placeholder_preset(profile):
        name = repository.get_default_play_name(conn, owner_id) or ""
    if not _real_name(name) or len(name) + len(profile["description"] or "") > PROFILE_TEXT_LIMIT:
        raise InvalidPlayNameError()
    if any(name_compare_key(c["name"] or "") == name_compare_key(name) for c in chars):
        raise InvalidPlayNameError("등장 캐릭터와 같은 이름을 사용할 수 없습니다.")
    return name


def _render_message(row, name: str) -> MessageResponse:
    message = _message(row)
    message.body = message.body.replace(USER_PLACEHOLDER, name)
    if message.speaker_kind == "user":
        message.speaker_name = name
    return message


def create_session(
    conn: Connection, owner_id: int, story_id: int, request: SessionCreateRequest
) -> SessionCreatedResponse:
    with conn.transaction():
        # 제작 저장·삭제와 동일한 스토리 행 잠금으로 저장본 세대를 고정한다.
        if repository.get_story(conn, story_id, owner_id, lock=True) is None:
            raise StoryNotFoundError()
        # 기본 이름이나 원본 선택지가 바뀌어도 최초의 검증된 요청으로 재시도를 찾는다.
        payload = json.dumps(request.model_dump(), sort_keys=True, separators=(",", ":"))
        fingerprint = sha256(payload.encode("ascii")).digest()
        existing_id = repository.get_initial_session_id(conn, story_id, owner_id, fingerprint)
        if existing_id is not None:
            return SessionCreatedResponse(id=existing_id)
        if not any(row["id"] == request.intro_id for row in repository.list_intros(conn, story_id)):
            raise IntroNotFoundError()
        profile = repository.get_selected_profile(
            conn, story_id, owner_id, request.profile_kind, request.profile_id
        )
        if profile is None or not _eligible_profile(profile):
            raise ProfileNotFoundError()
        name = _play_name(
            conn, owner_id, request, profile, repository.list_characters(conn, story_id)
        )
        snapshot = [
            _render_message(row, name).model_dump()
            for row in repository.list_intro_messages(conn, story_id, request.intro_id)
        ]
        session_id = repository.insert_session(
            conn, story_id, owner_id, request.intro_id, snapshot, fingerprint
        )
        repository.insert_applied_profile(conn, session_id, profile, name)
        repository.copy_scene_state(conn, session_id, request.intro_id)
        repository.copy_character_states(conn, session_id, story_id, request.intro_id)
        repository.copy_affinities(conn, session_id)
        repository.copy_relationships(conn, session_id)
    return SessionCreatedResponse(id=session_id)


def get_session(conn: Connection, owner_id: int, session_id: int) -> SessionResponse:
    with conn.transaction():
        repository.set_consistent_read(conn)
        session = repository.get_session(conn, session_id, owner_id)
        if session is None:
            raise SessionNotFoundError()
        return SessionResponse(
            id=session_id,
            story_title=session["story_title"] or "",
            # 실제 모델 설정/호출은 KAN-17에서 연결한다.
            model=None,
            read_only=session["read_only"],
            messages=session["intro_messages_snapshot"],
            characters=[
                CharacterResponse(id=row["id"], name=row["name"] or "")
                for row in repository.list_session_characters(conn, session_id)
            ],
        )
