"""CHAT-US-02 메시지 전송·응답 생성·이력 조회 규칙."""

import logging
from time import monotonic, sleep

from psycopg import Connection
from psycopg.errors import UniqueViolation

from app.core.config import settings
from app.core.exceptions import AppError
from app.features.conversation.messaging import providers, repository, worker
from app.features.conversation.messaging.info import (
    DATE_FIELDS,
    apply_info,
    format_info_lines,
    parse_info,
    replace_info,
    scene_view,
    split_info,
)
from app.features.conversation.messaging.parser import JS_TRIM_CHARS, parse_assistant_text
from app.features.conversation.messaging.prompt import (
    PromptContext,
    PromptHistoryMessage,
    PromptPart,
    build_messages,
)
from app.features.conversation.messaging.schemas import (
    ChatMessageResponse,
    CurrentSceneResponse,
    GenerationResponse,
    GenerationRetryRequest,
    MessagePartRequest,
    MessagePartResponse,
    MessageSendRequest,
    SceneInfoResponse,
    SceneInfoUpdateRequest,
    SceneInfoUpdateResponse,
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


# CHAT-US-08 info 이용자 수정(KAN-40 결정 C)


class MessageNotFoundError(AppError):
    status = 404
    code = "CONVERSATION_MESSAGE_NOT_FOUND"
    title = "대화에서 메시지를 찾을 수 없습니다"


class InfoNotAssistantMessageError(AppError):
    status = 422
    code = "SCENE_INFO_NOT_ASSISTANT_MESSAGE"
    title = "AI 응답의 장면 정보만 고칠 수 있습니다"


class InfoReplyNotCompletedError(AppError):
    status = 409
    code = "SCENE_INFO_REPLY_NOT_COMPLETED"
    title = "정상 완료된 AI 응답의 장면 정보만 고칠 수 있습니다"


class InfoNotLatestReplyError(AppError):
    status = 409
    code = "SCENE_INFO_NOT_LATEST_REPLY"
    title = "마지막 AI 응답의 장면 정보만 고칠 수 있습니다"


class InfoDateModeMismatchError(AppError):
    status = 422
    code = "SCENE_INFO_DATE_MODE_MISMATCH"
    title = "이 대화의 날짜 방식은 바꿀 수 없습니다"


class InfoUnknownCharacterError(AppError):
    status = 422
    code = "SCENE_INFO_UNKNOWN_CHARACTER"
    title = "이 대화에 등장하는 캐릭터가 아닙니다"


class InfoInvalidValueError(AppError):
    status = 422
    code = "SCENE_INFO_INVALID_VALUE"
    title = "장면 정보 값이 올바르지 않습니다"


# ---- 응답 변환 ----


class _Names:
    """응답의 speaker_name: 유저는 세션 적용 이름, 캐릭터는 세션 캐릭터 이름.

    CHAT-US-08: info를 읽을 때 쓰는 세션 날짜 방식도 함께 둔다.
    """

    def __init__(self, conn: Connection, session_id: int) -> None:
        self.play_name = repository.get_play_name(conn, session_id)
        self.characters = {
            row["id"]: row["name"] or ""
            for row in repository.list_session_characters(conn, session_id)
        }
        scene = repository.get_scene(conn, session_id)
        self.date_mode = scene["date_mode"] if scene else None

    def of(self, part: dict) -> str | None:
        if part["speaker_kind"] == "user":
            return self.play_name
        if part["speaker_kind"] == "character":
            return self.characters.get(part.get("character_id"))
        return None


def _info_response(text: str | None, names: _Names) -> SceneInfoResponse | None:
    """CHAT-US-08: 응답 원문 끝 info 블록(원문과 읽은 값). info가 없으면 None."""
    if text is None:
        return None
    _, info = split_info(text)
    if info is None:
        return None
    parsed = parse_info(info, names.date_mode, list(names.characters.values()))
    return SceneInfoResponse(text=info, **parsed)


def _chat_message(row, parts: list, names: _Names, text: str | None = None) -> ChatMessageResponse:
    """text는 assistant 메시지의 생성 원문이다(info를 읽는다). 이용자 메시지는 None."""
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
            for part in parts
        ],
        created_at=row["created_at"],
        info=_info_response(text, names),
    )


def _load_message(
    conn: Connection, session_id: int, message_id: int, names: _Names, text: str | None = None
):
    row = repository.get_message(conn, session_id, message_id)
    parts = repository.list_parts(conn, session_id, [message_id])[message_id]
    return _chat_message(row, parts, names, text)


def _generation_response(
    conn: Connection, row, names: _Names | None = None, latest_id: int | None = None
) -> GenerationResponse:
    """row는 get_generation_with_text 결과(offset 이후 원문 포함)."""
    names = names or _Names(conn, row["session_id"])
    assistant = None
    # 저장까지 끝난(completed) 응답만 메시지로 보여 준다. 진행·실패 중에는 text로만 전달한다.
    if row["status"] == "completed":
        assistant = _load_message(
            conn,
            row["session_id"],
            row["assistant_message_id"],
            names,
            repository.get_full_text(conn, row["id"]),
        )
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
        model=row["model_name"],
    )


def _load_generation(conn: Connection, generation_id: int, offset: int = 0) -> GenerationResponse:
    return _generation_response(
        conn, repository.get_generation_with_text(conn, generation_id, offset)
    )


# ---- 보이는 글 판단(CHAT-US-08: info만 있는 응답은 빈 응답) ----


def _session_character_names(conn: Connection, session_id: int) -> list[str]:
    """앱이 말풍선 태그로 인정하는 이름(세션 캐릭터 이름). 모델 요청의 등장 캐릭터 이름과 같다."""
    return [row["name"] or "" for row in repository.list_session_characters(conn, session_id)]


def has_visible_text(text: str, character_names: list[str]) -> bool:
    """응답 원문에 이용자에게 보이는 글이 있는가. 빈 응답·실패 상태·청구를 정하는 하나뿐인 기준.

    맨 끝 info 블록을 뗀 본문(info.py 규칙 1~6)을 앱과 같은 규칙(세션 캐릭터 이름, JS trim)으로
    말풍선으로 나눴을 때 한 부분이라도 남으면 보인다. 받은 글자 수로 정하지 않는다. 그래서 info만
    받고 끝나거나 끊긴 응답은 어느 경로(완료·공급자 오류·출력 한도·중단 정리·내부 오류)에서도 보이는
    글이 없는 응답(failed, 청구 없음, 재시도 가능)이 된다.
    """
    body, _ = split_info(text)
    return bool(parse_assistant_text(body, character_names, trim_chars=JS_TRIM_CHARS))


def _failure_status(text: str, character_names: list[str]) -> str:
    """실패한 생성의 상태: 보이는 글이 있으면 failed_partial(재시도 불가), 없으면 failed."""
    return "failed_partial" if has_visible_text(text, character_names) else "failed"


def _expire_stale(conn: Connection, session_id: int) -> None:
    """서버 중단 등으로 갱신이 멈춘 진행 중 생성을 실패로 정리해 세션이 막히지 않게 한다.

    상태는 받은 글자 수가 아니라 보이는 글(has_visible_text)로 정한다.
    """
    with conn.transaction():
        stale = repository.lock_stale_generations(
            conn, session_id, settings.chat_generation_stale_seconds
        )
        if not stale:
            return
        names = _session_character_names(conn, session_id)
        for row in stale:
            repository.expire_generation(conn, row["id"], _failure_status(row["text"], names))


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
    session,
    request_id: str,
    user_parts: list[dict] | None = None,
    user_message_id: int | None = None,
) -> int:
    """세션 행을 잠근 트랜잭션 안에서 호출한다. 진행 중 생성이 있으면 부분 유일 인덱스가 막는다.

    이용자 메시지(completed)와 응답을 받을 assistant 메시지(pending)를 세션의 다음 순서로 만들고
    생성 행에 연결한다. 재시도는 기존 이용자 메시지를 다시 쓰고 assistant 메시지만 새로 만든다.
    """
    session_id = session["id"]
    _expire_stale(conn, session_id)
    try:
        with conn.transaction():
            if user_message_id is None:
                user_message_id = repository.insert_message(conn, session_id, "user", "completed")
                repository.insert_parts(conn, session_id, user_message_id, user_parts or [])
            assistant_message_id = repository.insert_message(
                conn, session_id, "assistant", "pending"
            )
            return repository.insert_generation(
                conn,
                session_id,
                request_id,
                user_message_id,
                assistant_message_id,
                session["selected_model_id"],
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
        existing = repository.get_generation_by_request(conn, session_id, request.request_id)
        if existing is not None:
            return _load_generation(conn, existing["id"])
        if session["read_only"]:
            raise ConversationReadOnlyError()
        parts = _validate_parts(conn, session_id, request.parts)
        created_id = _insert_generation(conn, session, request.request_id, user_parts=parts)
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
        existing = repository.get_generation_by_request(conn, session_id, request.request_id)
        if existing is not None:
            return _load_generation(conn, existing["id"])
        if session["read_only"]:
            raise ConversationReadOnlyError()
        _expire_stale(conn, session_id)
        target = repository.get_generation(conn, generation_id, owner_id)
        latest = repository.get_latest_generation(conn, session_id)
        # 내용 없이 실패한 최신 요청만 이용자가 직접 다시 시도한다. 자동 재호출은 없다(AC15·16).
        if target["status"] != "failed" or latest["id"] != target["id"]:
            raise GenerationNotRetryableError()
        created_id = _insert_generation(
            conn, session, request.request_id, user_message_id=target["user_message_id"]
        )
        response = _load_generation(conn, created_id)
    worker.submit(run_generation, created_id)
    return response


# ---- 조회 ----


def get_generation(
    owner_id: int, generation_id: int, offset: int, wait: float
) -> GenerationResponse:
    """진행 중이고 offset 뒤 새 원문이 없으면 wait초까지 짧게 다시 확인한다(롱 폴링).

    요청 커넥션을 받지 않는다. 확인할 때마다 풀에서 커넥션을 빌렸다가 바로 돌려주고, 기다리는
    동안에는 커넥션을 붙잡지 않는다. 동시에 기다리는 폴링이 많아도 다른 요청이 풀을 쓸 수 있다.
    """
    with worker.connect() as conn:
        row = repository.get_generation(conn, generation_id, owner_id)
        if row is None:
            raise GenerationNotFoundError()
        if row["status"] in repository.ACTIVE_STATUSES:
            _expire_stale(conn, row["session_id"])
    deadline = monotonic() + wait
    while True:
        with worker.connect() as conn:
            row = repository.get_generation_with_text(conn, generation_id, offset)
            if (
                row["status"] not in repository.ACTIVE_STATUSES
                or row["text_length"] > offset
                or monotonic() >= deadline
            ):
                with conn.transaction():
                    return _generation_response(conn, row)
        sleep(POLL_INTERVAL_SECONDS)


def get_turns(conn: Connection, owner_id: int, session_id: int) -> TurnsResponse:
    session = repository.get_session(conn, session_id, owner_id)
    if session is None:
        raise SessionNotFoundError()
    _expire_stale(conn, session_id)
    with conn.transaction():
        repository.set_consistent_read(conn)
        names = _Names(conn, session_id)
        rows = repository.list_messages(conn, session_id)
        parts = repository.list_parts(conn, session_id, [row["id"] for row in rows])
        completed = repository.list_completed_texts(conn, session_id)
        texts = {row["message_id"]: row["text"] for row in completed}
        messages = [
            _chat_message(row, parts[row["id"]], names, texts.get(row["id"])) for row in rows
        ]
        # CHAT-US-08: 다음 요청의 [현재 장면]과 같은 함수로 만든 누적 현재 장면(상태 카드용).
        scene, states = _current_scene(
            conn,
            session_id,
            repository.list_session_characters(conn, session_id),
            completed=completed,
        )
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
            # 세션의 현재 선택 모델(CHAT-US-13에서 바꿀 수 있음)의 표시 이름(models.name)과 id.
            model=session["model_name"],
            model_id=session["selected_model_id"],
            current_scene=CurrentSceneResponse(**scene_view(scene, states)),
        )


# ---- CHAT-US-08 info 이용자 수정(KAN-40 결정 C) ----

# 날짜 방식마다 쓰는 칸. 다른 방식의 칸에 값이 오면 거절한다.
_DATE_MODE_FIELDS = {
    "calendar": ("year", "month", "day"),
    "world": ("era", "year", "month", "day"),
    "elapsed": ("reference_event", "elapsed_day"),
}


def _reads_back(scene: dict | None, states: list[dict], date_mode, names: list[str]) -> dict:
    """값을 info 줄로 써서(format_info_lines) parse_info로 다시 읽은 결과. 검증 기준은 이것 하나다.

    모델이 쓴 info를 읽는 규칙(날짜 방식·범위·2월 28일·자리 표시 값·상한·줄바꿈·`|`)을 그대로 써서,
    다시 읽어 같은 값이 나오는 값만 받는다. 그래서 저장된 info를 다음 요청이 그대로 읽는다.
    """
    return parse_info("\n".join(format_info_lines(scene, states)), date_mode, names)


def _edited_scene(
    scene: dict | None, states: list[dict], request: SceneInfoUpdateRequest
) -> tuple[dict, list[dict]]:
    """지금 장면(그 응답까지의 누적 값)에 요청 값을 덮어쓴 새 장면. 규칙에 맞지 않으면 422.

    빼거나 null인 칸은 지금 값을 둔다. 값을 지우는 방법은 없다.
    """
    scene = dict(scene or {})
    states = [dict(state) for state in states]
    date_mode = scene.get("date_mode")
    names = [state["name"] for state in states if state["name"]]
    if (date := request.date) is not None:
        if date_mode is None or (date.mode is not None and date.mode != date_mode):
            raise InfoDateModeMismatchError(field="date.mode", date_mode=date_mode)
        allowed = _DATE_MODE_FIELDS[date_mode]
        values = date.model_dump(include=set(DATE_FIELDS))
        for key in DATE_FIELDS:
            if key not in allowed and values[key] is not None:
                raise InfoInvalidValueError(
                    f"{date_mode} 날짜에는 {key} 값을 쓰지 않습니다.", field=f"date.{key}"
                )
        if date_mode == "elapsed" and values["reference_event"] is None:
            # info 규칙 11: 기준 사건을 쓰지 않으면 지금 기준 사건을 그대로 둔다.
            values["reference_event"] = scene.get("reference_event")
        wanted = {"mode": date_mode, **values}
        if _reads_back({"date_mode": date_mode, **values}, [], date_mode, [])["date"] != wanted:
            raise InfoInvalidValueError(
                "날짜가 비었거나 날짜 방식에 맞지 않습니다(달은 1~12, 2월은 28일까지).",
                field="date",
            )
        scene.update({key: values[key] for key in allowed})
    if (time := request.time) is not None:
        wanted = {"hour": time.hour, "minute": time.minute}
        if _reads_back(wanted, [], None, [])["time"] != wanted:
            raise InfoInvalidValueError("시는 0~23, 분은 0~59입니다.", field="time")
        scene.update(wanted)
    if request.place is not None:
        if _reads_back({"place": request.place}, [], None, [])["place"] != request.place:
            raise InfoInvalidValueError(
                "장소가 비었거나 자리 표시 값이거나 너무 깁니다(30자, 줄바꿈·앞뒤 공백 없이).",
                field="place",
            )
        scene["place"] = request.place
    by_name = {state["name"]: state for state in states if state["name"]}
    seen: set[str] = set()
    for index, item in enumerate(request.characters):
        if item.name not in by_name:
            raise InfoUnknownCharacterError(field=f"characters[{index}].name", name=item.name)
        if item.name in seen:
            raise InfoInvalidValueError(
                "같은 캐릭터를 두 번 보냈습니다.", field=f"characters[{index}].name"
            )
        seen.add(item.name)
        for key in ("clothing", "inner_thought"):
            if (value := getattr(item, key)) is None:
                continue
            read = _reads_back(None, [{"name": item.name, key: value}], None, names)["characters"]
            if not read or read[0][key] != value:
                raise InfoInvalidValueError(
                    "값이 비었거나 자리 표시 값이거나 너무 깁니다"
                    "(착장 300자·속마음 100자, 줄바꿈·앞뒤 공백 없이).",
                    field=f"characters[{index}].{key}",
                )
            by_name[item.name][key] = value
    return scene, states


def update_reply_info(
    conn: Connection,
    owner_id: int,
    session_id: int,
    message_id: int,
    request: SceneInfoUpdateRequest,
) -> SceneInfoUpdateResponse:
    """최신 완료 AI 응답의 info를 이용자가 고친다(KAN-40 결정 C: 크랙·제타처럼 상태 블록 수정).

    그 응답의 생성 원문에서 info 블록만 새 블록으로 바꾼다(없으면 맨 끝에 붙인다).
    말풍선(메시지 부분)·상태·청구·시각은 그대로다. 새 블록은 그 응답까지의 누적 장면에 수정 값을
    덮어쓴 전체 장면을 format_info_lines로 쓴 것이다. 다음 요청의 [현재 장면]과 턴 API
    current_scene은 같은 _current_scene이 이 원문을 다시 읽어 만든다(따로 저장하는 장면 값 없음).

    가장 뒤 완료 응답만 고칠 수 있다. 그래서 폼을 연 뒤 새 응답이 끝났으면 옛 응답 수정은 409가
    되고, 새 응답이 만든 장면을 옛 폼 값이 덮어쓰지 않는다. 진행 중 생성이 있으면 409다(그 요청이
    쓰는 장면과 어긋나지 않게). 전송·재시도·응답 저장과 같은 세션 행 잠금으로 줄 세운다.
    """
    with conn.transaction():
        session = repository.get_session(conn, session_id, owner_id, lock=True)
        if session is None:
            raise SessionNotFoundError()
        if session["read_only"]:
            raise ConversationReadOnlyError()
        _expire_stale(conn, session_id)
        active = repository.get_active_generation(conn, session_id)
        if active is not None:
            raise GenerationInProgressError(
                "응답이 끝난 뒤에 장면 정보를 고칠 수 있습니다.", active_generation_id=active["id"]
            )
        reply = repository.get_reply(conn, session_id, message_id)
        if reply is None:
            raise MessageNotFoundError()
        if reply["role"] != "assistant":
            raise InfoNotAssistantMessageError()
        if reply["generation_status"] != "completed":
            raise InfoReplyNotCompletedError()
        latest_id = repository.get_latest_completed_reply_id(conn, session_id)
        if latest_id != message_id:
            raise InfoNotLatestReplyError(latest_message_id=latest_id)
        session_characters = repository.list_session_characters(conn, session_id)
        # 이 응답이 가장 뒤 완료 응답이므로 그 응답까지의 누적 장면 = 지금 current_scene이다.
        scene, states = _current_scene(conn, session_id, session_characters)
        scene, states = _edited_scene(scene, states, request)
        text = replace_info(reply["text"], format_info_lines(scene, states))
        if not repository.update_completed_text(conn, reply["generation_id"], text):
            raise InfoReplyNotCompletedError()
        names = _Names(conn, session_id)
        scene, states = _current_scene(conn, session_id, session_characters)
        return SceneInfoUpdateResponse(
            message_id=message_id,
            info=_info_response(text, names),
            current_scene=CurrentSceneResponse(**scene_view(scene, states)),
        )


# ---- 생성 작업(요청과 별도 커넥션) ----


def _prompt_part(part: dict, names: _Names) -> PromptPart:
    return PromptPart(
        speaker_kind=part["speaker_kind"], speaker_name=names.of(part), body=part["body"]
    )


def _current_scene(
    conn: Connection,
    session_id: int,
    session_characters,
    before_position: int | None = None,
    completed=None,
) -> tuple[dict | None, list[dict]]:
    """CHAT-US-08 현재 장면과 캐릭터별 착장·속마음(모델 요청 [현재 장면]과 턴 API current_scene).

    세션 시작 값(conversation_scene_states·character_states, 첫 대화용)에서 출발해 정상 완료된
    응답 info를 오래된 것부터 덮어쓴다. info에 없는 항목은 앞 값을 잇는다. before_position이
    있으면 그 이용자 입력 앞의 응답만 쓴다(요청 때). 없으면 완료된 응답 전부(다음 요청이 쓸 값).
    completed는 이미 읽은 list_completed_texts 결과(같은 조건)다. 세션 시작 값 행은 바꾸지 않는다.
    """
    row = repository.get_scene(conn, session_id)
    scene = dict(row) if row else None
    states = [
        {
            "name": row["name"] or "",
            "clothing": row["clothing"],
            "inner_thought": row["inner_thought"],
        }
        for row in session_characters
    ]
    date_mode = scene["date_mode"] if scene else None
    names = [state["name"] for state in states]
    if completed is None:
        completed = repository.list_completed_texts(conn, session_id, before_position)
    for row in completed:
        _, info = split_info(row["text"])
        if info is not None:
            scene, states = apply_info(scene, states, parse_info(info, date_mode, names))
    return scene, states


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
    user_message = repository.get_message(conn, session_id, generation["user_message_id"])
    rows = repository.list_messages(conn, session_id, before_position=user_message["position"])
    parts = repository.list_parts(
        conn, session_id, [row["id"] for row in rows] + [user_message["id"]]
    )
    history = [
        PromptHistoryMessage(
            role=row["role"], parts=[_prompt_part(part, names) for part in parts[row["id"]]]
        )
        for row in rows
    ]
    expression = repository.get_expression(conn, story_id)
    scene, character_states = _current_scene(
        conn, session_id, session_characters, user_message["position"]
    )
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
        scene=scene,
        character_states=character_states,
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
        current=[_prompt_part(part, names) for part in parts[user_message["id"]]],
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


def _fail(
    generation_id: int, code: str, reported_model: str | None, character_names: list[str]
) -> None:
    """청구하지 않는 실패로 끝낸다. 상태는 지금까지 받은 원문의 보이는 글로 정한다."""
    with worker.connect() as conn:
        status = _failure_status(repository.get_full_text(conn, generation_id), character_names)
        repository.finish_generation(
            conn, generation_id, status, error_code=code, reported_model=reported_model
        )


def _finish_output_limit(
    generation_id: int, session_character_names: list[str], reported_model: str | None
) -> None:
    with worker.connect() as conn, conn.transaction():
        # CHAT-US-08: 맨 끝 info 블록은 보이는 글이 아니다(info만 있으면 청구하지 않는다).
        visible = has_visible_text(
            repository.get_full_text(conn, generation_id), session_character_names
        )
        repository.finish_generation(
            conn,
            generation_id,
            "failed_partial" if visible else "failed",
            error_code=providers.MODEL_OUTPUT_LIMIT,
            billable=visible,
            reported_model=reported_model,
        )


def _save_completed(generation_id: int, claimed, reported_model: str | None) -> None:
    """파싱한 발화자별 부분을 assistant 메시지에 저장하고 생성·메시지를 completed로 바꾼다.

    CHAT-US-08: 말풍선 부분에는 맨 끝 info 블록을 뗀 본문만 넣는다. 원문 전체(info 포함)는
    생성 원문에 그대로 남고, 다음 요청의 현재 장면과 이력 조회의 info는 그 원문에서 읽는다.
    """
    session_id = claimed["session_id"]
    with worker.connect() as conn, conn.transaction():
        repository.lock_session_by_id(conn, session_id)
        text, _ = split_info(repository.get_full_text(conn, generation_id))
        ids = _speaker_ids(conn, session_id)
        parts = [
            {
                "speaker_kind": part.speaker_kind,
                "character_id": ids.get(part.character_name) if part.character_name else None,
                "body": part.body,
            }
            for part in parse_assistant_text(text, list(ids))
        ]
        repository.insert_parts(conn, session_id, claimed["assistant_message_id"], parts)
        if not repository.finish_generation(
            conn, generation_id, "completed", billable=True, reported_model=reported_model
        ):
            # 그 사이 중단 정리된 생성이면 응답 저장도 되돌린다.
            raise RuntimeError("생성이 더 이상 진행 중이 아닙니다.")


def _stream(generation_id: int, claimed) -> None:
    with worker.connect() as conn, conn.transaction():
        repository.set_consistent_read(conn)
        context = load_prompt_context(conn, claimed)
        # 생성 행에 고정된 요청 모델로만 호출한다. 세션 모델을 바꿔도 진행 중 생성은 그대로다
        # (CHAT-US-13-AC02). 실패해도 다른 모델로 다시 호출하지 않는다(AC03).
        model = repository.get_model(conn, claimed["model_id"])
    request = providers.ProviderRequest(
        model=model["provider_model_id"],
        provider=model["provider"],
        messages=build_messages(context),
        character_names=context.session_character_names,
        last_user_text="\n".join(part.body for part in context.current),
    )
    names = context.session_character_names
    pending: list[str] = []
    # 첫 조각은 바로 기록하고 이후에는 최소 간격마다 모아 쓴다.
    last_flush = 0.0

    def flush() -> bool:
        nonlocal pending, last_flush
        last_flush = monotonic()
        if not pending:
            return True
        chunk = "".join(pending)
        pending = []
        with worker.connect() as conn:
            if not repository.append_text(conn, generation_id, chunk, request.reported_model):
                return False
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
        if not flush():
            return
        if exc.code == providers.MODEL_OUTPUT_LIMIT:
            _finish_output_limit(
                generation_id, context.session_character_names, request.reported_model
            )
        else:
            _fail(generation_id, exc.code, request.reported_model, names)
        return
    except Exception:
        logger.exception("응답 생성 공급자 오류: generation=%s", generation_id)
        flush()
        _fail(generation_id, "PROVIDER_ERROR", request.reported_model, names)
        return
    with worker.connect() as conn:
        text = repository.get_full_text(conn, generation_id)
    # CHAT-US-08: info 블록만 있는 응답도 빈 응답이다(완료·청구하지 않는다).
    if not has_visible_text(text, names):
        _fail(generation_id, "EMPTY_RESPONSE", request.reported_model, names)
        return
    try:
        _save_completed(generation_id, claimed, request.reported_model)
    except Exception:
        logger.exception("응답 저장 실패: generation=%s", generation_id)
        with worker.connect() as conn:
            repository.finish_generation(
                conn,
                generation_id,
                "save_failed",
                error_code="SAVE_FAILED",
                reported_model=request.reported_model,
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
                    status = _failure_status(
                        repository.get_full_text(conn, generation_id),
                        _session_character_names(conn, row["session_id"]),
                    )
                    repository.finish_generation(
                        conn, generation_id, status, error_code="INTERNAL_ERROR"
                    )
        except Exception:
            logger.exception("응답 생성 실패 기록 오류: generation=%s", generation_id)
