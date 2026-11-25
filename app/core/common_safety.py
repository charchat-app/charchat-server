"""CREATE-US-08-AC15 공통 안전(공통 제공 금지) 판정 경계. 주입할 때만 켜진다.

등급(SAFE/UNSAFE)·언세이프 자격·계정 접근과 별개로,
공통 금지로 판정된 저장본은 관람자에게 제공하지 않는다.
이 모듈은 판정기를 부르는 동기 경계와 판정기에 넘길 정본(저장본) 텍스트 읽기만 맡는다.
실제 판정기(공급자 호출)·보관 정책·롤아웃은 정해지지 않았으므로
기본 앱은 판정기를 주입하지 않고 기존 동작을 유지한다.

- 판정 결과는 캐시하지 않는다.
  응답마다 그 응답과 같은 스냅숏(또는 잠금 저장 직전 재확인)으로 판정한다.
- `CLEAR` 멤버만 통과다.
  차단·판정 불가·예외·다른 타입 값은 모두 제공하지 않는다(fail closed).
- 로그에는 원문·판정기 예외 메시지를 남기지 않는다.

여러 기능(대화 시작·태그 검색)이 같은 정본 범위를 판정하도록
저장본 읽기를 여기 한 곳에 둔다.
다른 기능의 repository를 직접 import하지 않기 위한 공통 읽기다.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from psycopg import Connection

logger = logging.getLogger(__name__)

USER_SLOT = "{{user}}"


class CommonSafety(StrEnum):
    CLEAR = "clear"
    BLOCKED = "blocked"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class SafetyMessage:
    speaker_kind: str
    speaker_name: str | None
    body: str


@dataclass(frozen=True, slots=True)
class SafetyIntro:
    title: str
    messages: tuple[SafetyMessage, ...]


@dataclass(frozen=True, slots=True)
class SafetyCharacter:
    name: str
    description: str


@dataclass(frozen=True, slots=True)
class SafetyRelationship:
    source: str  # 캐릭터 이름 또는 극 중 유저 자리 `{{user}}`
    target: str
    label: str


@dataclass(frozen=True, slots=True)
class SafetyProfile:
    name: str
    description: str


@dataclass(frozen=True, slots=True)
class SafetySurface:
    """판정기에 넘기는 정본 텍스트.

    응답에 실제로 묶이는 인트로(`bound_intro`)와 세션 캐릭터 이름도 포함한다.
    """

    story_id: int
    title: str
    subtitle: str
    world_description: str
    tags: tuple[str, ...]
    characters: tuple[SafetyCharacter, ...]
    relationships: tuple[SafetyRelationship, ...]
    intros: tuple[SafetyIntro, ...]
    # 세션 시작·조회 응답에 실제로 나가는 렌더링된 인트로
    # (플레이 이름 치환 결과).
    bound_intro: tuple[SafetyMessage, ...] = ()
    # 세션 조회 응답에 나가는 세션 캐릭터 이름 스냅숏.
    bound_character_names: tuple[str, ...] = ()
    # 같은 스냅숏의 제작자 프리셋 이름·설명 정본 전체(모든 판정 면에서 읽는다).
    # 플레이어 개인 프로필은 제작자 정본이 아니므로 넣지 않는다.
    bound_profiles: tuple[SafetyProfile, ...] = ()
    # 세션 시작·조회 응답에 묶이는 실제 플레이 이름(재시도·조회는 저장된 값).
    bound_play_name: str | None = None
    # 세션 유래 판정(대화 시작 사전 판정·세션 조회)인가. 입력 오류로 바인딩 값이 없어도 True다.
    session_origin: bool = False


CommonSafetyGuard = Callable[[SafetySurface], CommonSafety]


def is_clear(guard: CommonSafetyGuard, surface: SafetySurface) -> bool:
    """판정기가 `CommonSafety.CLEAR` 멤버를 돌려줄 때만 True.

    그 외는 모두 제공하지 않는다.
    """
    try:
        outcome = guard(surface)
    except Exception as exc:  # 판정기 장애는 통과가 아니다.
        # 예외 메시지에 원문이 섞일 수 있어 타입 이름만 남긴다.
        logger.warning(
            "common safety evaluation unavailable story_id=%s error=%s",
            surface.story_id,
            type(exc).__name__,
        )
        return False
    if outcome is CommonSafety.CLEAR:
        return True
    if not isinstance(outcome, CommonSafety):
        logger.warning("common safety evaluation invalid story_id=%s", surface.story_id)
    return False


def safety_message(raw) -> SafetyMessage:
    return SafetyMessage(
        speaker_kind=raw["speaker_kind"],
        speaker_name=raw["speaker_name"],
        body=raw["body"] or "",
    )


def read_story_surface(
    conn: Connection,
    story_id: int,
    *,
    bound_intro: tuple[SafetyMessage, ...] = (),
    bound_character_names: tuple[str, ...] = (),
    bound_play_name: str | None = None,
    session_origin: bool = False,
) -> SafetySurface | None:
    """저장본 정본 텍스트를 읽는다. 호출자의 트랜잭션(스냅숏·잠금)을 그대로 쓴다.

    공개 범위·삭제 여부는 보지 않는다. 접근 판단은 호출 기능이 먼저 한다.
    임시 저장본(working copy)은 관람자에게 나가지 않으므로 읽지 않는다.
    관계는 null이 아닌 양 끝이 모두 이 스토리의 캐릭터일 때만 읽고, null 끝은 `{{user}}`로 둔다.
    제작자 프리셋은 응답 노출 여부와 무관하게 전부 읽고, 개인 프로필은 읽지 않는다.
    """
    story = conn.execute(
        "SELECT title, subtitle, world_description FROM stories WHERE id = %s", (story_id,)
    ).fetchone()
    if story is None:
        return None
    tags = conn.execute(
        """SELECT t.name FROM story_tags st JOIN tags t ON t.id = st.tag_id
           WHERE st.story_id = %s ORDER BY t.id""",
        (story_id,),
    ).fetchall()
    characters = conn.execute(
        "SELECT name, description FROM characters WHERE story_id = %s ORDER BY position, id",
        (story_id,),
    ).fetchall()
    relationships = conn.execute(
        """SELECT coalesce(src.name, %s) AS source, coalesce(dst.name, %s) AS target, r.label
           FROM character_initial_relationships r
           LEFT JOIN characters src ON src.id = r.source_character_id
           LEFT JOIN characters dst ON dst.id = r.target_character_id
           WHERE (r.source_character_id IS NOT NULL OR r.target_character_id IS NOT NULL)
             AND (r.source_character_id IS NULL OR src.story_id = %s)
             AND (r.target_character_id IS NULL OR dst.story_id = %s)
           ORDER BY r.id""",
        (USER_SLOT, USER_SLOT, story_id, story_id),
    ).fetchall()
    presets = conn.execute(
        """SELECT name, description FROM story_user_presets
           WHERE story_id = %s ORDER BY position, id""",
        (story_id,),
    ).fetchall()
    intros = conn.execute(
        "SELECT id, title FROM intros WHERE story_id = %s ORDER BY position, id", (story_id,)
    ).fetchall()
    messages: dict[int, list[SafetyMessage]] = {row["id"]: [] for row in intros}
    for row in conn.execute(
        """SELECT i.id AS intro_id, m.speaker_kind, c.name AS speaker_name, m.body
           FROM intros i JOIN intro_messages m ON m.intro_id = i.id
           LEFT JOIN characters c ON c.id = m.character_id AND c.story_id = i.story_id
           WHERE i.story_id = %s ORDER BY i.position, m.position, m.id""",
        (story_id,),
    ):
        messages[row["intro_id"]].append(safety_message(row))
    return SafetySurface(
        story_id=story_id,
        title=story["title"] or "",
        subtitle=story["subtitle"] or "",
        world_description=story["world_description"] or "",
        tags=tuple(row["name"] for row in tags),
        characters=tuple(
            SafetyCharacter(name=row["name"] or "", description=row["description"] or "")
            for row in characters
        ),
        relationships=tuple(
            SafetyRelationship(source=row["source"], target=row["target"], label=row["label"] or "")
            for row in relationships
        ),
        intros=tuple(
            SafetyIntro(title=row["title"] or "", messages=tuple(messages[row["id"]]))
            for row in intros
        ),
        bound_intro=bound_intro,
        bound_character_names=bound_character_names,
        bound_profiles=tuple(
            SafetyProfile(name=row["name"] or "", description=row["description"] or "")
            for row in presets
        ),
        bound_play_name=bound_play_name,
        session_origin=session_origin,
    )


def story_is_clear(
    conn: Connection,
    guard: CommonSafetyGuard,
    story_id: int,
    *,
    bound_intro: tuple[SafetyMessage, ...] = (),
    bound_character_names: tuple[str, ...] = (),
    bound_play_name: str | None = None,
    session_origin: bool = False,
) -> bool:
    """정본을 읽어 판정한다. 정본이 없으면 제공하지 않는다."""
    surface = read_story_surface(
        conn,
        story_id,
        bound_intro=bound_intro,
        bound_character_names=bound_character_names,
        bound_play_name=bound_play_name,
        session_origin=session_origin,
    )
    return surface is not None and is_clear(guard, surface)
