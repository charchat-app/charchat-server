"""CREATE-US-08-AC15 세션 안전 조합 판정기(주입 전용, 기본 앱 꺼짐).

- 로컬 검사기가 먼저 전체 표면(세션 바인딩 값 포함)을 본다. 로컬 검사기는 외부로 보내지 않는
  판정기여야 한다(이 모듈은 그 구현을 정하지 않는다).
- 로컬이 `CLEAR` 멤버를 돌려줄 때만 외부 판정기를 부르며, 외부에는 허용 목록 필드만 새로 복사한
  정본(`canonical_surface`)만 넘긴다. 렌더링 인트로·플레이 이름·세션 캐릭터 이름·적용 프로필
  (개인 프로필일 수 있음)·세션 유래 표식은 외부로 나가지 않는다.
- 형식이 어긋난 표면, 로컬 검사기 부재, 세션 유래 판정인데 바인딩 값(플레이 이름·적용 프로필)이
  없거나, 세션 유래가 아닌데 바인딩 값이 섞인 표면은 어느 판정기도 부르지 않고 판정 불가로 닫는다.
- 예외를 내지 않는다. `CLEAR` 멤버만 통과이고 다른 값·예외는 닫힌 실패다.
- 로그를 남기지 않는다. 원문·예외 메시지는 보관·출력하지 않는다.
"""

from app.core.common_safety import (
    CommonSafety,
    CommonSafetyGuard,
    SafetyCharacter,
    SafetyIntro,
    SafetyMessage,
    SafetyProfile,
    SafetyRelationship,
    SafetySurface,
)


def _str(value) -> bool:
    return type(value) is str


def _tuple_of(value, check) -> bool:
    return type(value) is tuple and all(check(item) for item in value)


def _message(value) -> bool:
    return (
        type(value) is SafetyMessage
        and _str(value.speaker_kind)
        and (value.speaker_name is None or _str(value.speaker_name))
        and _str(value.body)
    )


def _character(value) -> bool:
    return type(value) is SafetyCharacter and _str(value.name) and _str(value.description)


def _relationship(value) -> bool:
    return (
        type(value) is SafetyRelationship
        and _str(value.source)
        and _str(value.target)
        and _str(value.label)
    )


def _intro(value) -> bool:
    return type(value) is SafetyIntro and _str(value.title) and _tuple_of(value.messages, _message)


def _profile(value) -> bool:
    return type(value) is SafetyProfile and _str(value.name) and _str(value.description)


def canonical_surface(surface) -> SafetySurface | None:
    """제작자 정본 필드만 새로 복사한 표면. 형식이 어긋나면 None."""
    if type(surface) is not SafetySurface:
        return None
    if not (
        type(surface.story_id) is int
        and _str(surface.title)
        and _str(surface.subtitle)
        and _str(surface.world_description)
        and _tuple_of(surface.tags, _str)
        and _tuple_of(surface.characters, _character)
        and _tuple_of(surface.relationships, _relationship)
        and _tuple_of(surface.intros, _intro)
        and _tuple_of(surface.bound_profiles, _profile)
    ):
        return None
    return SafetySurface(
        story_id=surface.story_id,
        title=surface.title,
        subtitle=surface.subtitle,
        world_description=surface.world_description,
        tags=surface.tags,
        characters=surface.characters,
        relationships=surface.relationships,
        intros=surface.intros,
        bound_profiles=surface.bound_profiles,
    )


def _bindings_valid(surface: SafetySurface) -> bool:
    """세션 유래면 바인딩 값이 모두 올바른 형식이고 플레이 이름·적용 프로필이 있어야 한다.
    세션 유래가 아니면 바인딩 값이 하나도 없어야 한다."""
    if surface.session_origin is True:
        return (
            _str(surface.bound_play_name)
            and surface.bound_play_name != ""
            and _profile(surface.bound_applied_profile)
            and _tuple_of(surface.bound_intro, _message)
            and _tuple_of(surface.bound_character_names, _str)
        )
    return (
        surface.session_origin is False
        and surface.bound_intro == ()
        and surface.bound_character_names == ()
        and surface.bound_play_name is None
        and surface.bound_applied_profile is None
    )


def _run(guard: CommonSafetyGuard, surface: SafetySurface) -> CommonSafety:
    try:
        outcome = guard(surface)
    except Exception:
        return CommonSafety.UNAVAILABLE
    if type(outcome) is not CommonSafety:
        return CommonSafety.UNAVAILABLE
    return outcome


class ComposedSessionSafetyGuard:
    """`CommonSafetyGuard`. 로컬 검사(전체 표면) → 외부 판정(정본만) 순서로 판정한다."""

    def __init__(self, local: CommonSafetyGuard | None, external: CommonSafetyGuard):
        self._local = local
        self._external = external

    def __repr__(self):
        return "ComposedSessionSafetyGuard(...)"

    def __call__(self, surface: SafetySurface) -> CommonSafety:
        if self._local is None:
            return CommonSafety.UNAVAILABLE
        try:
            canonical = canonical_surface(surface)
            if canonical is None or not _bindings_valid(surface):
                return CommonSafety.UNAVAILABLE
        except Exception:
            return CommonSafety.UNAVAILABLE
        local = _run(self._local, surface)
        if local is not CommonSafety.CLEAR:
            return local
        return _run(self._external, canonical)
