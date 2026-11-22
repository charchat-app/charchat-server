"""CHAT-US-02 메시지 전송·응답 생성·이력 조회 규칙."""

import logging
from time import monotonic, sleep

from psycopg import Connection
from psycopg.errors import UniqueViolation

from app.core.config import settings
from app.core.exceptions import AppError
from app.features.conversation.messaging import providers, repository, worker
from app.features.conversation.messaging.parser import parse_assistant_text
from app.features.conversation.messaging.prompt import (
    PromptContext,
    PromptHistoryMessage,
    PromptPart,
    build_messages,
)
from app.features.conversation.messaging.schemas import (
    ChatMessageResponse,
    GenerationResponse,
    GenerationRetryRequest,
    MessagePartRequest,
    MessagePartResponse,
    MessageSendRequest,
    TurnsResponse,
)

logger = logging.getLogger(__name__)

# 유저·캐릭터·나레이터 본문 합산 한도(공백·줄바꿈·서식 기호 포함, 코드 포인트 기준, AC04).
MESSAGE_TEXT_LIMIT = 5000
POLL_INTERVAL_SECONDS = 0.1


class SessionNotFoundError(AppError):
    status = 404
    code = "CONVERSATION_SESSION_NOT_FOUND"
    title = "접근할 수 있는 대화 세션을 찾을 수 없습니다"


class GenerationNotFoundError(AppError):
    status = 404
    code = "GENERATION_NOT_FOUND"
    title = "응답 생성 요청을 찾을 수 없습니다"


class MessageEmptyError(AppError):
    status = 422
    code = "MESSAGE_EMPTY"
    title = "보낼 내용을 입력해 주세요"


class MessageTooLongError(AppError):
    status = 422
    code = "MESSAGE_TOO_LONG"
    title = f"메시지는 합쳐서 {MESSAGE_TEXT_LIMIT:,}자까지 보낼 수 있습니다"


class InvalidSpeakerError(AppError):
    status = 422
    code = "MESSAGE_INVALID_SPEAKER"
    title = "이 대화에서 선택할 수 없는 발화자입니다"


class ConversationReadOnlyError(AppError):
    status = 409
    code = "CONVERSATION_READ_ONLY"
    title = "삭제된 스토리의 대화는 이어서 진행할 수 없습니다"


class GenerationInProgressError(AppError):
    status = 409
    code = "GENERATION_IN_PROGRESS"
    title = "이전 응답이 끝난 뒤에 보낼 수 있습니다"


class GenerationNotRetryableError(AppError):
    status = 409
    code = "GENERATION_NOT_RETRYABLE"
    title = "이 응답은 다시 시도할 수 없습니다"


# ---- 응답 변환 ----


class _Names:
    """응답의 speaker_name: 유저는 세션 적용 이름, 캐릭터는 세션 캐릭터 이름."""

    def __init__(self, conn: Connection, session_id: int) -> None:
        self.play_name = repository.get_play_name(conn, session_id)
        self.characters = {
            row["id"]: row["name"] or ""
            for row in repository.list_session_characters(conn, session_id)
        }

    def of(self, part: dict) -> str | None:
        if part["speaker_kind"] == "user":
            return self.play_name
        if part["speaker_kind"] == "character":
            return self.characters.get(part.get("character_id"))
        return None


def _chat_message(row, names: _Names) -> ChatMessageResponse:
    return ChatMessageResponse(
        id=row["id"],
        role=row["role"],
        parts=[
            MessagePartResponse(
                speaker_kind=part["speaker_kind"],
                character_id=part.get("character_id"),
                speaker_name=names.of(part),
                body=part["body"],
            )
            for part in row["parts"]
        ],
        created_at=row["created_at"],
    )


def _generation_response(
    conn: Connection, row, names: _Names | None = None, latest_id: int | None = None
) -> GenerationResponse:
    """row는 get_generation_with_text 결과(offset 이후 원문 포함)."""
    names = names or _Names(conn, row["session_id"])
    assistant = None
    if row["assistant_message_id"] is not None:
        assistant = _chat_message(repository.get_message(conn, row["assistant_message_id"]), names)
    retryable = False
    if row["status"] == "failed":
        if latest_id is None:
            latest_id = repository.get_latest_generation(conn, row["session_id"])["id"]
        retryable = latest_id == row["id"]
    return GenerationResponse(
        id=row["id"],
        session_id=row["session_id"],
        status=row["status"],
        user_message_id=row["user_message_id"],
        assistant_message=assistant,
        text=row["text"],
        next_offset=row["text_length"],
        error_code=row["error_code"],
        retryable=retryable,
    )


def _load_generation(conn: Connection, generation_id: int, offset: int = 0) -> GenerationResponse:
    return _generation_response(
        conn, repository.get_generation_with_text(conn, generation_id, offset)
    )


# ---- 전송 ----


def _validate_parts(conn: Connection, session_id: int, parts: list[MessagePartRequest]):
    if not parts or all(not part.body.strip() for part in parts):
        raise MessageEmptyError()
    if sum(len(part.body) for part in parts) > MESSAGE_TEXT_LIMIT:
        raise MessageTooLongError()
    characters = {row["id"] for row in repository.list_session_characters(conn, session_id)}
    stored = []
    for part in parts:
        if part.speaker_kind == "character":
            if part.character_id not in characters:
                raise InvalidSpeakerError()
        elif part.character_id is not None:
            raise InvalidSpeakerError("유저·나레이터 발화에는 캐릭터를 지정하지 않습니다.")
        # 입력은 자르거나 다듬지 않고 작성 순서 그대로 저장한다(AC02·AC16).
        stored.append(
            {
                "speaker_kind": part.speaker_kind,
                "character_id": part.character_id,
                "body": part.body,
            }
        )
    return stored


def _insert_generation(
    conn: Connection,
    session_id: int,
    owner_id: int,
    request_id: str,
    user_parts: list[dict] | None = None,
    user_message_id: int | None = None,
) -> int:
    """세션 행을 잠근 트랜잭션 안에서 호출한다. 진행 중 생성이 있으면 부분 유일 인덱스가 막는다."""
    repository.expire_stale_generations(conn, session_id, settings.chat_generation_stale_seconds)
    model = repository.set_session_model_if_missing(conn, session_id, settings.chat_default_model)
    try:
        with conn.transaction():
            if user_message_id is None:
                user_message_id = repository.insert_message(
                    conn, session_id, "user", user_parts or []
                )
            return repository.insert_generation(
                conn, session_id, owner_id, request_id, user_message_id, model
            )
    except UniqueViolation as exc:
        if exc.diag.constraint_name == repository.ACTIVE_INDEX:
            active = repository.get_active_generation(conn, session_id)
            raise GenerationInProgressError(
                active_generation_id=active["id"] if active else None
            ) from exc
        raise


def send_message(
    conn: Connection, owner_id: int, session_id: int, request: MessageSendRequest
) -> GenerationResponse:
    created_id = None
    with conn.transaction():
        # 같은 세션의 전송·재시도를 세션 행 잠금으로 줄 세워 중복 요청 확인을 확실히 한다(AC10).
        session = repository.get_session(conn, session_id, owner_id, lock=True)
        if session is None:
            raise SessionNotFoundError()
        existing = repository.get_generation_by_request(
            conn, owner_id, session_id, request.request_id
        )
        if existing is not None:
            return _load_generation(conn, existing["id"])
        if session["read_only"]:
            raise ConversationReadOnlyError()
        parts = _validate_parts(conn, session_id, request.parts)
        created_id = _insert_generation(
            conn, session_id, owner_id, request.request_id, user_parts=parts
        )
        response = _load_generation(conn, created_id)
    # 커밋 뒤에 시작한다. 응답은 시작 전 상태(pending)로 돌려준다.
    worker.submit(run_generation, created_id)
    return response


def retry_generation(
    conn: Connection, owner_id: int, generation_id: int, request: GenerationRetryRequest
) -> GenerationResponse:
    with conn.transaction():
        target = repository.get_generation(conn, generation_id, owner_id)
        if target is None:
            raise GenerationNotFoundError()
        session_id = target["session_id"]
        session = repository.get_session(conn, session_id, owner_id, lock=True)
        if session is None:
            raise GenerationNotFoundError()
        existing = repository.get_generation_by_request(
            conn, owner_id, session_id, request.request_id
        )
        if existing is not None:
            return _load_generation(conn, existing["id"])
        if session["read_only"]:
            raise ConversationReadOnlyError()
        repository.expire_stale_generations(
            conn, session_id, settings.chat_generation_stale_seconds
        )
        target = repository.get_generation(conn, generation_id, owner_id)
        latest = repository.get_latest_generation(conn, session_id)
        # 내용 없이 실패한 최신 요청만 이용자가 직접 다시 시도한다. 자동 재호출은 없다(AC15·16).
        if target["status"] != "failed" or latest["id"] != target["id"]:
            raise GenerationNotRetryableError()
        created_id = _insert_generation(
            conn,
            session_id,
            owner_id,
            request.request_id,
            user_message_id=target["user_message_id"],
        )
        response = _load_generation(conn, created_id)
    worker.submit(run_generation, created_id)
    return response


# ---- 조회 ----


def get_generation(
    conn: Connection, owner_id: int, generation_id: int, offset: int, wait: float
) -> GenerationResponse:
    """진행 중이고 offset 뒤 새 원문이 없으면 wait초까지 짧게 다시 확인한다(롱 폴링)."""
    row = repository.get_generation(conn, generation_id, owner_id)
    if row is None:
        raise GenerationNotFoundError()
    if row["status"] in repository.ACTIVE_STATUSES:
        repository.expire_stale_generations(
            conn, row["session_id"], settings.chat_generation_stale_seconds
        )
    deadline = monotonic() + wait
    while True:
        row = repository.get_generation_with_text(conn, generation_id, offset)
        if (
            row["status"] not in repository.ACTIVE_STATUSES
            or row["text_length"] > offset
            or monotonic() >= deadline
        ):
            break
        sleep(POLL_INTERVAL_SECONDS)
    with conn.transaction():
        return _generation_response(conn, row)


def get_turns(conn: Connection, owner_id: int, session_id: int) -> TurnsResponse:
    session = repository.get_session(conn, session_id, owner_id)
    if session is None:
        raise SessionNotFoundError()
    repository.expire_stale_generations(conn, session_id, settings.chat_generation_stale_seconds)
    with conn.transaction():
        repository.set_consistent_read(conn)
        names = _Names(conn, session_id)
        messages = [_chat_message(row, names) for row in repository.list_messages(conn, session_id)]
        latest = repository.get_latest_generation(conn, session_id)
        latest_id = latest["id"] if latest else None
        active = None
        failed = None
        if latest is not None and latest["status"] in repository.ACTIVE_STATUSES:
            active = _generation_response(
                conn, repository.get_generation_with_text(conn, latest["id"], 0), names, latest_id
            )
        elif (
            latest is not None
            and latest["status"] in repository.FAILED_STATUSES
            and not repository.has_user_message_after(conn, session_id, latest["user_message_id"])
        ):
            failed = _generation_response(
                conn, repository.get_generation_with_text(conn, latest["id"], 0), names, latest_id
            )
        return TurnsResponse(
            messages=messages,
            active_generation=active,
            last_failed_generation=failed,
            model=session["model"] or settings.chat_default_model,
        )


# ---- 생성 작업(요청과 별도 커넥션) ----


def _prompt_part(part: dict, names: _Names) -> PromptPart:
    return PromptPart(
        speaker_kind=part["speaker_kind"], speaker_name=names.of(part), body=part["body"]
    )


def load_prompt_context(conn: Connection, generation) -> PromptContext:
    """생성 때마다 최신 저장 설정과 세션 현재값을 다시 읽는다(KAN-16 AC29).

    제작 임시 저장본(story_working_copies)은 읽지 않는다.
    """
    session_id = generation["session_id"]
    session = repository.get_session(conn, session_id, generation["owner_id"])
    story_id = session["story_id"]
    story = repository.get_story_basics(conn, story_id)
    names = _Names(conn, session_id)
    profile = repository.get_user_profile(conn, session_id)
    play_name = profile["name"] if profile else ""
    fewshots: dict[int, list[PromptPart]] = {}
    for row in repository.list_fewshot_messages(conn, story_id):
        fewshots.setdefault(row["fewshot_id"], []).append(
            PromptPart(row["speaker_kind"], row["speaker_name"], row["body"] or "")
        )
    session_characters = repository.list_session_characters(conn, session_id)
    user_message = repository.get_message(conn, generation["user_message_id"])
    history = [
        PromptHistoryMessage(
            role=row["role"], parts=[_prompt_part(part, names) for part in row["parts"]]
        )
        for row in repository.list_messages(conn, session_id, before_seq=user_message["seq"])
    ]
    expression = repository.get_expression(conn, story_id)
    return PromptContext(
        title=story["title"] or "",
        subtitle=story["subtitle"] or "",
        world_description=story["world_description"] or "",
        tags=repository.list_tags(conn, story_id),
        characters=[
            {"name": row["name"] or "", "description": row["description"] or ""}
            for row in repository.list_story_characters(conn, story_id)
        ],
        expression=dict(expression) if expression else {},
        fewshots=list(fewshots.values()),
        intro=[
            PromptPart(m["speaker_kind"], m.get("speaker_name"), m.get("body") or "")
            for m in session["intro_messages_snapshot"]
        ],
        play_name=play_name,
        profile_description=(profile["description"] if profile else None) or "",
        scene=dict(scene) if (scene := repository.get_scene(conn, session_id)) else None,
        character_states=[
            {
                "name": row["name"] or "",
                "clothing": row["clothing"],
                "inner_thought": row["inner_thought"],
            }
            for row in session_characters
        ],
        affinities=[
            {
                "source": row["source"] or "",
                "target": play_name if row["target_kind"] == "user" else row["target"] or "",
                "value": row["value"],
            }
            for row in repository.list_affinities(conn, session_id)
        ],
        relationships=[
            {
                "source": play_name if row["source_kind"] == "user" else row["source"] or "",
                "target": play_name if row["target_kind"] == "user" else row["target"] or "",
                "label": row["label"],
            }
            for row in repository.list_relationships(conn, session_id)
        ],
        session_character_names=[row["name"] or "" for row in session_characters],
        history=history,
        current=[_prompt_part(part, names) for part in user_message["parts"]],
    )


def _speaker_ids(conn: Connection, session_id: int) -> dict[str, int]:
    """파서 이름 → 세션 캐릭터 ID. 세션 이름을 우선하고 원본의 최신 저장 이름도 받는다."""
    mapping: dict[str, int] = {}
    rows = repository.list_session_characters(conn, session_id)
    for row in rows:
        if row["source_name"]:
            mapping.setdefault(row["source_name"], row["id"])
    for row in rows:
        if row["name"]:
            mapping[row["name"]] = row["id"]
    return mapping


def _fail(generation_id: int, streamed: int, code: str) -> None:
    status = "failed" if streamed == 0 else "failed_partial"
    with worker.connect() as conn:
        repository.finish_generation(conn, generation_id, status, error_code=code)


def _save_completed(generation_id: int, session_id: int) -> None:
    with worker.connect() as conn, conn.transaction():
        repository.lock_session_by_id(conn, session_id)
        text = repository.get_full_text(conn, generation_id)
        ids = _speaker_ids(conn, session_id)
        parts = [
            {
                "speaker_kind": part.speaker_kind,
                "character_id": ids.get(part.character_name) if part.character_name else None,
                "body": part.body,
            }
            for part in parse_assistant_text(text, list(ids))
        ]
        message_id = repository.insert_message(conn, session_id, "assistant", parts)
        if not repository.finish_generation(
            conn, generation_id, "completed", assistant_message_id=message_id, billable=True
        ):
            # 그 사이 중단 정리된 생성이면 응답 저장도 되돌린다.
            raise RuntimeError("생성이 더 이상 진행 중이 아닙니다.")


def _stream(generation_id: int, claimed) -> None:
    with worker.connect() as conn, conn.transaction():
        repository.set_consistent_read(conn)
        context = load_prompt_context(conn, claimed)
    request = providers.ProviderRequest(
        model=claimed["model"] or settings.chat_default_model,
        messages=build_messages(context),
        character_names=context.session_character_names,
        last_user_text="\n".join(part.body for part in context.current),
    )
    streamed = 0
    pending: list[str] = []
    # 첫 조각은 바로 기록하고 이후에는 최소 간격마다 모아 쓴다.
    last_flush = 0.0

    def flush() -> bool:
        nonlocal streamed, pending, last_flush
        last_flush = monotonic()
        if not pending:
            return True
        chunk = "".join(pending)
        pending = []
        with worker.connect() as conn:
            if not repository.append_text(conn, generation_id, chunk):
                return False
        streamed += len(chunk)
        return True

    try:
        for chunk in providers.get_provider().stream(request):
            pending.append(chunk)
            if monotonic() - last_flush >= settings.chat_stream_flush_ms / 1000:
                if not flush():
                    return
        if not flush():
            return
    except providers.ProviderError as exc:
        flush()
        _fail(generation_id, streamed, exc.code)
        return
    except Exception:
        logger.exception("응답 생성 공급자 오류: generation=%s", generation_id)
        flush()
        _fail(generation_id, streamed, "PROVIDER_ERROR")
        return
    with worker.connect() as conn:
        text = repository.get_full_text(conn, generation_id)
    if not text.strip():
        _fail(generation_id, 0, "EMPTY_RESPONSE")
        return
    try:
        _save_completed(generation_id, claimed["session_id"])
    except Exception:
        logger.exception("응답 저장 실패: generation=%s", generation_id)
        with worker.connect() as conn:
            repository.finish_generation(
                conn, generation_id, "save_failed", error_code="SAVE_FAILED"
            )


def run_generation(generation_id: int) -> None:
    """pending 생성 하나를 끝까지 처리한다. 화면 이탈·연결 단절로 취소하지 않는다(AC12~14)."""
    try:
        with worker.connect() as conn:
            claimed = repository.mark_streaming(conn, generation_id)
        if claimed is None:
            return
        _stream(generation_id, claimed)
    except Exception:
        logger.exception("응답 생성 작업 오류: generation=%s", generation_id)
        try:
            with worker.connect() as conn:
                row = repository.get_generation(conn, generation_id)
                if row is not None:
                    status = "failed" if row["text_length"] == 0 else "failed_partial"
                    repository.finish_generation(
                        conn, generation_id, status, error_code="INTERNAL_ERROR"
                    )
        except Exception:
            logger.exception("응답 생성 실패 기록 오류: generation=%s", generation_id)
