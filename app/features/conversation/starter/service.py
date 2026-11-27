"""CHAT-US-01 저장본만 조회하고 대화 시작 규칙을 적용한다."""

import json
from collections import defaultdict
from dataclasses import dataclass
from hashlib import sha256

from psycopg import Connection

from app.core import common_safety
from app.core.common_safety import CommonSafetyGuard, SafetyMessage, SafetyProfile
from app.core.config import settings
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


def get_story(
    conn: Connection, owner_id: int, story_id: int, guard: CommonSafetyGuard | None = None
) -> StoryResponse:
    with conn.transaction():
        repository.set_consistent_read(conn)
        story = repository.get_story(conn, story_id, owner_id)
        if story is None:
            raise StoryNotFoundError()
        profiles = [
            row
            for row in repository.list_profiles(conn, story_id, owner_id)
            if _eligible_profile(row)
        ]
        # CREATE-US-08-AC15: 응답과 같은 스냅숏의 정본(제작자 프리셋 전체 포함)을
        # 판정한다. 비공개 소유자도 우회하지 않는다.
        if guard is not None and not common_safety.story_is_clear(conn, guard, story_id):
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
                for row in profiles
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


class ModelNotConfiguredError(AppError):
    status = 503
    code = "CONVERSATION_MODEL_NOT_CONFIGURED"
    title = "대화에 사용할 기본 모델이 설정되지 않았습니다"


def _default_model_id(conn: Connection) -> int:
    """CHAT_DEFAULT_MODEL_NAME과 이름이 같은 models 행 하나. 없거나 여러 개면 시작하지 않는다."""
    ids = repository.list_model_ids_by_name(conn, settings.chat_default_model_name)
    if len(ids) != 1:
        raise ModelNotConfiguredError(
            f"기본 모델 '{settings.chat_default_model_name}'의 models 행이 {len(ids)}개입니다."
        )
    return ids[0]


def _play_name(conn: Connection, owner_id: int, request: SessionCreateRequest, profile, chars):
    name = request.play_name
    if name == "" and _is_placeholder_preset(profile):
        name = repository.get_default_play_name(conn, owner_id) or ""
    if not _real_name(name):
        raise InvalidPlayNameError()
    if len(name) + len(profile["description"] or "") > PROFILE_TEXT_LIMIT:
        # 기본 플레이 이름(최대 20자)도 긴 프로필 설명과 합치면 넘을 수 있어 할 일을 알려 준다.
        raise InvalidPlayNameError(
            f"플레이 이름과 프로필 설명을 합쳐 {PROFILE_TEXT_LIMIT:,}자를 넘을 수 없어요. "
            "플레이 이름을 줄여 주세요."
        )
    if any(name_compare_key(c["name"] or "") == name_compare_key(name) for c in chars):
        raise InvalidPlayNameError("등장 캐릭터와 같은 이름을 사용할 수 없습니다.")
    return name


def _render_message(row, name: str) -> MessageResponse:
    message = _message(row)
    message.body = message.body.replace(USER_PLACEHOLDER, name)
    if message.speaker_kind == "user":
        message.speaker_name = name
    return message


@dataclass
class _Start:
    """잠금 전 사전 판정과 잠금 후 저장이 같은 규칙으로 계산하는 시작 준비 결과."""

    fingerprint: bytes
    existing_id: int | None
    profile: dict | None = None
    name: str | None = None
    snapshot: list[dict] | None = None


def _prepare_start(
    conn: Connection, owner_id: int, story_id: int, request: SessionCreateRequest, *, lock: bool
) -> _Start:
    # 제작 저장·삭제와 동일한 스토리 행 잠금으로 저장본 세대를 고정한다.
    if repository.get_story(conn, story_id, owner_id, lock=lock) is None:
        raise StoryNotFoundError()
    # 기본 이름이나 원본 선택지가 바뀌어도 최초의 검증된 요청으로 재시도를 찾는다.
    payload = json.dumps(request.model_dump(), sort_keys=True, separators=(",", ":"))
    fingerprint = sha256(payload.encode("ascii")).digest()
    existing_id = repository.get_initial_session_id(conn, story_id, owner_id, fingerprint)
    if existing_id is not None:
        return _Start(fingerprint, existing_id)
    if not any(row["id"] == request.intro_id for row in repository.list_intros(conn, story_id)):
        raise IntroNotFoundError()
    profile = repository.get_selected_profile(
        conn, story_id, owner_id, request.profile_kind, request.profile_id, lock=lock
    )
    if profile is None or not _eligible_profile(profile):
        raise ProfileNotFoundError()
    name = _play_name(conn, owner_id, request, profile, repository.list_characters(conn, story_id))
    snapshot = [
        _render_message(row, name).model_dump()
        for row in repository.list_intro_messages(conn, story_id, request.intro_id)
    ]
    return _Start(fingerprint, None, profile, name, snapshot)


def _bound_intro(rows) -> tuple[SafetyMessage, ...]:
    return tuple(common_safety.safety_message(row) for row in rows)


def _applied_profile(name: str, description: str | None) -> SafetyProfile:
    """응답에 묶이는 적용 프로필(적용 이름·설명). 저장값과 같은 규칙으로 만든다."""
    return SafetyProfile(name=name, description=description or "")


def _start_surface(conn: Connection, story_id: int, start: _Start | None, *, lock: bool = False):
    """시작 응답에 묶이는 정본. 재시도면 기존 세션에 저장된 인트로·플레이 이름을 판정한다.

    lock=True는 잠금 후 재확인에서만 쓴다. 저장된 세션·적용 프로필 행을 잠근 채 읽어,
    비교 뒤 응답 전까지 판정한 값이 바뀌지 않게 한다.
    """
    bound: tuple[SafetyMessage, ...] = ()
    play_name = None
    applied = None
    if start is not None and start.existing_id is not None:
        # 재시도는 원본 프로필이 아니라 저장된 적용 프로필 스냅숏을 판정한다.
        existing = repository.get_session_bound(conn, start.existing_id, lock=lock)
        if existing is None:
            # 저장된 세션이나 적용 프로필이 없으면 판정한 값과 같다고 볼 수 없다(닫힘).
            return None
        bound = _bound_intro(existing["intro_messages_snapshot"])
        play_name = existing["play_name"]
        applied = _applied_profile(play_name, existing["applied_description"])
    elif start is not None:
        bound = _bound_intro(start.snapshot)
        play_name = start.name
        applied = _applied_profile(start.name, start.profile["description"])
    # 입력 오류(start=None)로 바인딩 값이 없어도 세션 유래 판정이다.
    return common_safety.read_story_surface(
        conn,
        story_id,
        bound_intro=bound,
        bound_play_name=play_name,
        bound_applied_profile=applied,
        session_origin=True,
    )


_START_INPUT_ERRORS = (IntroNotFoundError, ProfileNotFoundError, InvalidPlayNameError)


def _preflight_surface(conn: Connection, owner_id: int, story_id: int, request):
    """CREATE-US-08-AC15 잠금 전 사전 판정용 정본. 판정기 호출 동안 스토리 잠금을 잡지 않는다.

    선택 입력이 틀리면 정본만 판정한다(차단 스토리는 입력 오류보다 먼저 미노출로 닫는다).
    잠금 후에는 같은 입력 오류가 다시 나거나, 그 사이 입력이 유효해졌다면 정본이 달라 닫힌다.
    """
    with conn.transaction():
        repository.set_consistent_read(conn)
        try:
            start = _prepare_start(conn, owner_id, story_id, request, lock=False)
        except _START_INPUT_ERRORS:
            start = None
        return _start_surface(conn, story_id, start)


def create_session(
    conn: Connection,
    owner_id: int,
    story_id: int,
    request: SessionCreateRequest,
    guard: CommonSafetyGuard | None = None,
) -> SessionCreatedResponse:
    expected = None
    if guard is not None:
        expected = _preflight_surface(conn, owner_id, story_id, request)
        if expected is None or not common_safety.is_clear(guard, expected):
            raise StoryNotFoundError()
    with conn.transaction():
        start = _prepare_start(conn, owner_id, story_id, request, lock=True)
        # 잠금 후 정본이 판정한 것과 다르면 판정하지 않은 내용으로 시작하지 않는다.
        # (통과 재사용 금지)
        if expected is not None and _start_surface(conn, story_id, start, lock=True) != expected:
            raise StoryNotFoundError()
        if start.existing_id is not None:
            return SessionCreatedResponse(id=start.existing_id)
        # 세션 시작 때 기본 모델을 고정한다(KAN-16 AC20). 이후 생성은 이 모델을 쓴다.
        model_id = _default_model_id(conn)
        session_id = repository.insert_session(
            conn,
            story_id,
            owner_id,
            request.intro_id,
            start.snapshot,
            start.fingerprint,
            model_id,
        )
        repository.insert_applied_profile(conn, session_id, start.profile, start.name)
        repository.copy_scene_state(conn, session_id, request.intro_id)
        repository.copy_character_states(conn, session_id, story_id, request.intro_id)
        repository.copy_affinities(conn, session_id)
        repository.copy_relationships(conn, session_id)
    return SessionCreatedResponse(id=session_id)


def get_session(
    conn: Connection, owner_id: int, session_id: int, guard: CommonSafetyGuard | None = None
) -> SessionResponse:
    with conn.transaction():
        repository.set_consistent_read(conn)
        session = repository.get_session(conn, session_id, owner_id)
        if session is None:
            raise SessionNotFoundError()
        characters = repository.list_session_characters(conn, session_id)
        # CREATE-US-08-AC15: 현재 정본과 이 응답에 나가는 세션 인트로·캐릭터 이름을 함께 판정한다.
        if guard is not None and not common_safety.story_is_clear(
            conn,
            guard,
            session["story_id"],
            bound_intro=_bound_intro(session["intro_messages_snapshot"]),
            bound_character_names=tuple(row["name"] or "" for row in characters),
            bound_play_name=session["play_name"],
            bound_applied_profile=_applied_profile(
                session["play_name"], session["applied_description"]
            ),
            session_origin=True,
        ):
            raise SessionNotFoundError()
        return SessionResponse(
            id=session_id,
            story_title=session["story_title"] or "",
            # 세션에 고정된 모델의 표시 이름(models.name, 예: 루나).
            model=session["model"],
            read_only=session["read_only"],
            # 대화 입력창의 기본 발화자 이름(CHAT-US-02-AC01).
            play_name=session["play_name"],
            messages=session["intro_messages_snapshot"],
            characters=[
                CharacterResponse(id=row["id"], name=row["name"] or "") for row in characters
            ],
        )
