"""CHAT-US-08 장면 유지·변화(KAN-22). 설계: KAN-40 초안(나래님 확인 전 추천안으로 구현).

- 정상 완료된 턴에서만 장면을 바꾼다. 응답 저장과 같은 트랜잭션에서 적용해, 응답이 저장되지 않으면
  장면도 바뀌지 않는다(실패·일부 실패·저장 실패 턴은 무변경).
- 바뀐 값마다 변경 기록(전·후 상태, 근거 메시지·문장)을 남긴다.
  재생성 후보 전환(10번)·구간 삭제(12번)·응답 수정(11번)에서 '그때의 장면'으로 돌아갈 때 쓴다.
- 이용자도 현재 장면을 직접 고칠 수 있다(11-27 결정 C). 고친 값은 다음 전송의 [현재 장면]에 쓰이고,
  AI 판단은 그 위에서 이어 바꾼다. 직접 고친 것도 변경 기록(source='user_edit')으로 남긴다.
"""

import logging

from psycopg import Connection

from app.core.config import settings
from app.core.exceptions import AppError
from app.features.conversation.scene import repository
from app.features.conversation.scene.rules import (
    CLOTHING_MAX_LENGTH,
    DAYS_IN_MONTH,
    INNER_THOUGHT_MAX_LENGTH,
    PLACE_MAX_LENGTH,
    REFERENCE_EVENT_MAX_LENGTH,
    SCENE_FIELDS,
    YEAR_MAX,
    ScenePatch,
    apply_patch,
)
from app.features.conversation.scene.schemas import (
    SceneCharacterResponse,
    SceneResponse,
    SceneUpdateRequest,
)

logger = logging.getLogger(__name__)


def _state(scene: dict, characters: list[dict]) -> dict:
    return {
        "scene": {key: scene.get(key) for key in SCENE_FIELDS},
        "characters": [
            {
                "id": row["id"],
                "name": row["name"],
                "clothing": row["clothing"],
                "inner_thought": row["inner_thought"],
            }
            for row in characters
        ],
    }


def apply_turn_change(
    conn: Connection,
    *,
    session_id: int,
    generation_id: int,
    user_message_id: int,
    assistant_message_id: int,
    patch: ScenePatch | None,
) -> int | None:
    """정상 완료 저장 트랜잭션 안에서 장면 변화를 적용한다. 바뀐 것이 없으면 None.

    적용 중 오류가 나도 응답 저장은 지킨다(세이브포인트로 장면 변경만 되돌린다).
    """
    if patch is None:
        return None
    try:
        with conn.transaction():
            row = repository.get_scene_for_update(conn, session_id)
            if row is None:
                return None
            scene = dict(row)
            characters = [
                dict(r) for r in repository.list_character_states_for_update(conn, session_id)
            ]
            new_scene, changed = apply_patch(scene, characters, patch)
            if new_scene == {key: scene.get(key) for key in SCENE_FIELDS} and not changed:
                return None
            if new_scene != {key: scene.get(key) for key in SCENE_FIELDS}:
                repository.update_scene(conn, session_id, new_scene)
            for state in changed:
                repository.update_character_state(conn, session_id, state)
            by_id = {row["id"]: row for row in changed}
            after_characters = [by_id.get(row["id"], row) for row in characters]
            return repository.insert_change(
                conn,
                session_id=session_id,
                generation_id=generation_id,
                assistant_message_id=assistant_message_id,
                evidence_message_id=(
                    user_message_id if patch.evidence_from == "user" else assistant_message_id
                ),
                evidence_from=patch.evidence_from,
                evidence=patch.evidence,
                before=_state(scene, characters),
                after=_state(new_scene, after_characters),
            )
    except Exception:
        logger.exception("장면 변경 적용 실패: generation=%s", generation_id)
        return None


# ---- 이용자 직접 조회·수정 ----

# 생성 4번(initial_state LIMITS)과 같은 글자 상한. 연호는 판단이 바꾸지 않아 여기서만 쓴다.
ERA_MAX_LENGTH = 10
# 생성 때의 1~9999는 입력 범위이고 대화 중 상한이 아니다(생성 4 상세). 대화 중에는 AI 판단으로
# 9999를 넘을 수 있으므로, 화면이 받은 값을 그대로 다시 보내도 거절하지 않게 열 범위까지 받는다.
ELAPSED_DAY_MAX = 2**31 - 1
TEXT_LIMITS = {
    "place": PLACE_MAX_LENGTH,
    "era": ERA_MAX_LENGTH,
    "reference_event": REFERENCE_EVENT_MAX_LENGTH,
    "clothing": CLOTHING_MAX_LENGTH,
    "inner_thought": INNER_THOUGHT_MAX_LENGTH,
}
DATE_FIELDS = ("era", "year", "month", "day", "reference_event", "elapsed_day")
# 날짜 방식별로 쓰는 칸. 방식이 없는 세션은 날짜를 다루지 않는다.
DATE_MODE_FIELDS = {
    "calendar": ("year", "month", "day"),
    "world": ("era", "year", "month", "day"),
    "elapsed": ("reference_event", "elapsed_day"),
    None: (),
}


class SessionNotFoundError(AppError):
    status = 404
    code = "CONVERSATION_SESSION_NOT_FOUND"
    title = "접근할 수 있는 대화 세션을 찾을 수 없습니다"


class ConversationReadOnlyError(AppError):
    status = 409
    code = "CONVERSATION_READ_ONLY"
    title = "삭제된 스토리의 대화는 이어서 진행할 수 없습니다"


class GenerationInProgressError(AppError):
    # 전송 API(messaging)와 같은 코드. 진행 중 턴의 장면 판단이 고친 값을 덮어쓰지 않게 막는다.
    status = 409
    code = "GENERATION_IN_PROGRESS"
    title = "이전 응답이 끝난 뒤에 장면을 고칠 수 있습니다"


class SceneInvalidError(AppError):
    status = 422
    code = "CONVERSATION_SCENE_INVALID"
    title = "장면 값을 확인해 주세요"


class SceneCharacterInvalidError(AppError):
    status = 422
    code = "CONVERSATION_SCENE_CHARACTER_INVALID"
    title = "이 대화에서 고칠 수 없는 캐릭터입니다"


def _issue(field: str, code: str) -> dict:
    return {"field": field, "code": code}


def _clean_text(value: str | None, field: str, limit: int, issues: list[dict]) -> str | None:
    """앞뒤 공백을 지우고 빈 문자열은 미지정(None)으로 본다. NUL·상한 초과는 문제로 모은다."""
    if value is None:
        return None
    if "\x00" in value or any("\ud800" <= ch <= "\udfff" for ch in value):
        # NUL·짝 없는 서로게이트는 DB에 저장할 수 없다(500 대신 422).
        issues.append(_issue(field, "SCENE_TEXT_INVALID"))
        return None
    value = value.strip()
    if not value:
        return None
    if len(value) > limit:
        issues.append(_issue(field, "SCENE_TEXT_TOO_LONG"))
    return value


def _in_range(values: dict, field: str, low: int, high: int, issues: list[dict]) -> bool:
    if values[field] is None:
        return False
    if not low <= values[field] <= high:
        issues.append(_issue(field, "SCENE_VALUE_OUT_OF_RANGE"))
        return False
    return True


def check_user_edit(date_mode: str | None, request: SceneUpdateRequest):
    """이용자 수정 값을 생성 4번과 같은 규칙으로 정리·검사한다(DB를 모르는 순수 함수).

    반환: (장면 값 dict(date_mode 제외), 캐릭터 수정 목록, 문제 목록[{"field", "code"}])
    - 날짜: 전부 미지정 또는 방식별 칸을 모두 채운다. 방식에 안 쓰는 칸에 값이 오면 문제다.
    - 시각: 시·분 둘 다 미지정 또는 둘 다(24시간제).
    """
    issues: list[dict] = []
    values = {
        "era": _clean_text(request.era, "era", TEXT_LIMITS["era"], issues),
        "year": request.year,
        "month": request.month,
        "day": request.day,
        "reference_event": _clean_text(
            request.reference_event, "reference_event", TEXT_LIMITS["reference_event"], issues
        ),
        "elapsed_day": request.elapsed_day,
        "hour": request.hour,
        "minute": request.minute,
        "place": _clean_text(request.place, "place", TEXT_LIMITS["place"], issues),
    }
    used = DATE_MODE_FIELDS.get(date_mode, ())
    issues += [
        _issue(field, "SCENE_FIELD_NOT_USED")
        for field in DATE_FIELDS
        if field not in used and values[field] is not None
    ]
    if any(values[field] is not None for field in used):
        issues += [
            _issue(field, "SCENE_DATE_INCOMPLETE") for field in used if values[field] is None
        ]
        if "year" in used:
            _in_range(values, "year", 1, YEAR_MAX, issues)
            month_ok = _in_range(values, "month", 1, 12, issues)
            last_day = DAYS_IN_MONTH[values["month"] - 1] if month_ok else 31
            _in_range(values, "day", 1, last_day, issues)
        if "elapsed_day" in used:
            _in_range(values, "elapsed_day", 1, ELAPSED_DAY_MAX, issues)
    if values["hour"] is not None or values["minute"] is not None:
        issues += [
            _issue(field, "SCENE_TIME_INCOMPLETE")
            for field in ("hour", "minute")
            if values[field] is None
        ]
        _in_range(values, "hour", 0, 23, issues)
        _in_range(values, "minute", 0, 59, issues)
    characters = []
    for index, row in enumerate(request.characters):
        path = f"characters[{index}]"
        characters.append(
            {
                "id": row.id,
                "clothing": _clean_text(
                    row.clothing, f"{path}.clothing", TEXT_LIMITS["clothing"], issues
                ),
                "inner_thought": _clean_text(
                    row.inner_thought,
                    f"{path}.inner_thought",
                    TEXT_LIMITS["inner_thought"],
                    issues,
                ),
            }
        )
    return values, characters, issues


def _character_issues(requested: list[dict], session_ids: set[int]) -> list[dict]:
    """캐릭터 ID는 이 세션의 캐릭터 상태여야 하고 한 번만 올 수 있다."""
    issues = []
    seen = set()
    for index, row in enumerate(requested):
        if row["id"] not in session_ids:
            issues.append(_issue(f"characters[{index}].id", "SCENE_CHARACTER_NOT_FOUND"))
        elif row["id"] in seen:
            issues.append(_issue(f"characters[{index}].id", "SCENE_CHARACTER_DUPLICATE"))
        seen.add(row["id"])
    return issues


def _response(scene: dict, characters: list[dict], read_only: bool) -> SceneResponse:
    return SceneResponse(
        **{key: scene.get(key) for key in SCENE_FIELDS},
        characters=[
            SceneCharacterResponse(
                id=row["id"],
                name=row["name"],
                clothing=row["clothing"],
                inner_thought=row["inner_thought"],
            )
            for row in characters
        ],
        read_only=read_only,
    )


def get_scene(conn: Connection, owner_id: int, session_id: int) -> SceneResponse:
    """세션의 현재 장면. 잠그지 않고 한 시점의 값을 읽는다."""
    with conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        session = repository.get_session(conn, session_id, owner_id)
        if session is None:
            raise SessionNotFoundError()
        row = repository.get_scene(conn, session_id)
        characters = [dict(r) for r in repository.list_character_states(conn, session_id)]
    return _response(dict(row) if row else {}, characters, session["read_only"])


def update_scene(
    conn: Connection, owner_id: int, session_id: int, request: SceneUpdateRequest
) -> SceneResponse:
    """이용자가 현재 장면을 직접 고친다(전체 교체, 목록에 없는 캐릭터는 그대로).

    잠금 순서는 전송·재시도·모델 변경과 같다(세션 → 장면). 진행 중 생성이 있으면 막는다:
    그 턴이 끝나며 저장하는 장면 판단이 고친 값을 덮어쓰지 않게 하기 위해서다.
    """
    with conn.transaction():
        session = repository.get_session(conn, session_id, owner_id, lock=True)
        if session is None:
            raise SessionNotFoundError()
        if session["read_only"]:
            raise ConversationReadOnlyError()
        # 전송·재시도와 같이 멈춘 생성은 먼저 실패로 정리한다. 정리하지 않고 지나치면 아직 살아 있는
        # 작업이 나중에 완료 저장하며 고치기 전 장면으로 판단한 결과를 덮어쓸 수 있다.
        repository.expire_stale_generations(
            conn, session_id, settings.chat_generation_stale_seconds
        )
        if repository.has_active_generation(conn, session_id):
            raise GenerationInProgressError()
        row = repository.get_scene_for_update(conn, session_id)
        if row is None:
            # 세션 생성 트랜잭션이 장면 행을 항상 만든다. 없으면 고칠 대상이 없는 세션으로 본다.
            raise SessionNotFoundError()
        scene = dict(row)
        characters = [
            dict(r) for r in repository.list_character_states_for_update(conn, session_id)
        ]
        values, requested, issues = check_user_edit(scene["date_mode"], request)
        character_issues = _character_issues(requested, {r["id"] for r in characters})
        # 문제는 한 번에 모두 알려 준다. 캐릭터 ID 문제가 하나라도 있으면 캐릭터 코드로 응답한다.
        if character_issues:
            raise SceneCharacterInvalidError(issues=issues + character_issues)
        if issues:
            raise SceneInvalidError(issues=issues)
        new_scene = {**{key: scene.get(key) for key in SCENE_FIELDS}, **values}
        by_id = {r["id"]: r for r in requested}
        after_characters = [{**r, **by_id[r["id"]]} if r["id"] in by_id else r for r in characters]
        changed = [new for new, old in zip(after_characters, characters, strict=True) if new != old]
        scene_changed = new_scene != {key: scene.get(key) for key in SCENE_FIELDS}
        if scene_changed or changed:
            if scene_changed:
                repository.update_scene(conn, session_id, new_scene)
            for state in changed:
                repository.update_character_state(conn, session_id, state)
            repository.insert_user_edit_change(
                conn,
                session_id=session_id,
                before=_state(scene, characters),
                after=_state(new_scene, after_characters),
            )
    return _response(new_scene, after_characters, False)
