"""CREATE-US-08-AC15 세션 안전 조합(로컬 검사 먼저 → 외부엔 허용 목록 정본만) 단위 테스트.

외부 판정기는 기록용 가짜다. 실제 공급자를 부르지 않는다.
"""

import dataclasses
import json

import pytest

from app.core.common_safety import (
    CommonSafety,
    SafetyCharacter,
    SafetyIntro,
    SafetyMessage,
    SafetyProfile,
    SafetyRelationship,
    SafetySurface,
)
from app.core.session_safety import ComposedSessionSafetyGuard, canonical_surface
from app.features.creation.save_visibility.jev_decisions import DecisionsReply
from app.features.creation.save_visibility.jev_story_safety import (
    CHOICES,
    QUESTION,
    JevStorySafetyGuard,
    build_request,
)

PLAY = "ZQX-PLAY-1101"
APPLIED_NAME = "ZQX-APPLIED-NAME-2202"
APPLIED_DESC = "ZQX-APPLIED-DESC-3303"
RENDERED = "ZQX-RENDERED-4404"
SNAPSHOT_NAME = "ZQX-SNAPSHOT-5505"
SESSION_VALUES = (PLAY, APPLIED_NAME, APPLIED_DESC, RENDERED, SNAPSHOT_NAME)

CANONICAL = SafetySurface(
    story_id=7,
    title="Title",
    subtitle="Sub",
    world_description="World",
    tags=("tag",),
    characters=(SafetyCharacter("Aria", "Guard"),),
    relationships=(SafetyRelationship("{{user}}", "Aria", "Ally"),),
    intros=(SafetyIntro("Opening", (SafetyMessage("character", "Aria", "Hi {{user}}"),)),),
    bound_profiles=(SafetyProfile("{{user}}", "Preset"),),
)
SESSION = dataclasses.replace(
    CANONICAL,
    bound_intro=(SafetyMessage("character", "Aria", f"Hi {RENDERED}"),),
    bound_character_names=(SNAPSHOT_NAME,),
    bound_play_name=PLAY,
    bound_applied_profile=SafetyProfile(APPLIED_NAME, APPLIED_DESC),
    session_origin=True,
)


class Recorder:
    def __init__(self, outcome=CommonSafety.CLEAR):
        self.outcome = outcome
        self.surfaces = []

    def __call__(self, surface):
        self.surfaces.append(surface)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def test_local_sees_full_session_surface_and_external_only_canonical_CREATE_US_08_AC15():
    local, external = Recorder(), Recorder()
    assert ComposedSessionSafetyGuard(local, external)(SESSION) is CommonSafety.CLEAR
    assert local.surfaces == [SESSION]
    [sent] = external.surfaces
    assert sent == CANONICAL
    assert sent is not SESSION and type(sent) is SafetySurface
    assert sent.session_origin is False
    assert sent.bound_intro == () and sent.bound_character_names == ()
    assert sent.bound_play_name is None and sent.bound_applied_profile is None
    assert not any(value in repr(sent) for value in SESSION_VALUES)


@pytest.mark.parametrize(
    "outcome",
    [CommonSafety.BLOCKED, CommonSafety.UNAVAILABLE, RuntimeError("x"), "clear", None, True],
)
def test_local_non_clear_fails_closed_without_external_CREATE_US_08_AC15(outcome):
    local, external = Recorder(outcome), Recorder()
    result = ComposedSessionSafetyGuard(local, external)(SESSION)
    assert result is not CommonSafety.CLEAR and isinstance(result, CommonSafety)
    assert local.surfaces == [SESSION]
    assert external.surfaces == []


def test_missing_local_checker_fails_closed_without_external_CREATE_US_08_AC15():
    external = Recorder()
    guard = ComposedSessionSafetyGuard(None, external)
    assert guard(SESSION) is CommonSafety.UNAVAILABLE
    assert guard(CANONICAL) is CommonSafety.UNAVAILABLE
    assert external.surfaces == []


@pytest.mark.parametrize(
    "change",
    [
        {"bound_play_name": None},
        {"bound_play_name": ""},
        {"bound_play_name": 3},
        {"bound_applied_profile": None},
        {"bound_applied_profile": ("n", "d")},
        {"bound_applied_profile": SafetyProfile("n", None)},
        {"bound_intro": [SafetyMessage("character", "Aria", "x")]},
        {"bound_intro": (("character", "Aria", "x"),)},
        {"bound_intro": (SafetyMessage("character", "Aria", None),)},
        {"bound_character_names": ("ok", None)},
        # 입력 오류 사전 판정: 바인딩 값이 하나도 없다.
        {"bound_intro": (), "bound_play_name": None, "bound_applied_profile": None},
    ],
)
def test_invalid_bound_snapshot_fails_closed_without_any_call_CREATE_US_08_AC15(change):
    local, external = Recorder(), Recorder()
    surface = dataclasses.replace(SESSION, **change)
    assert ComposedSessionSafetyGuard(local, external)(surface) is CommonSafety.UNAVAILABLE
    assert local.surfaces == [] and external.surfaces == []


@pytest.mark.parametrize(
    "change",
    [
        {"bound_play_name": PLAY},
        {"bound_intro": SESSION.bound_intro},
        {"bound_character_names": (SNAPSHOT_NAME,)},
        {"bound_applied_profile": SafetyProfile(APPLIED_NAME, APPLIED_DESC)},
    ],
)
def test_non_session_surface_with_bound_values_is_refused_CREATE_US_08_AC15(change):
    """세션 유래 표식 없이 바인딩 값이 섞인 표면은 형식 오류로 닫는다."""
    local, external = Recorder(), Recorder()
    surface = dataclasses.replace(CANONICAL, **change)
    assert ComposedSessionSafetyGuard(local, external)(surface) is CommonSafety.UNAVAILABLE
    assert local.surfaces == [] and external.surfaces == []


def test_viewer_surface_runs_local_then_external_CREATE_US_08_AC15():
    local, external = Recorder(), Recorder()
    assert ComposedSessionSafetyGuard(local, external)(CANONICAL) is CommonSafety.CLEAR
    assert local.surfaces == [CANONICAL] and external.surfaces == [CANONICAL]


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (CommonSafety.BLOCKED, CommonSafety.BLOCKED),
        (CommonSafety.UNAVAILABLE, CommonSafety.UNAVAILABLE),
        (RuntimeError("down"), CommonSafety.UNAVAILABLE),
        ("clear", CommonSafety.UNAVAILABLE),
    ],
)
def test_external_non_clear_fails_closed_CREATE_US_08_AC15(outcome, expected):
    guard = ComposedSessionSafetyGuard(Recorder(), Recorder(outcome))
    assert guard(SESSION) is expected


def test_subclass_or_foreign_surface_is_refused_CREATE_US_08_AC15():
    class Sub(SafetySurface):
        __slots__ = ()

    local, external = Recorder(), Recorder()
    guard = ComposedSessionSafetyGuard(local, external)
    sub = Sub(**{f.name: getattr(SESSION, f.name) for f in dataclasses.fields(SESSION)})
    assert guard(sub) is CommonSafety.UNAVAILABLE
    assert guard(object()) is CommonSafety.UNAVAILABLE
    assert canonical_surface(object()) is None
    assert local.surfaces == [] and external.surfaces == []


def test_canonical_surface_rejects_malformed_creator_fields_CREATE_US_08_AC15():
    bad = dataclasses.replace(CANONICAL, characters=(("Aria", "Guard"),))
    assert canonical_surface(bad) is None
    assert canonical_surface(dataclasses.replace(CANONICAL, tags=["tag"])) is None
    assert canonical_surface(dataclasses.replace(CANONICAL, title=None)) is None


# --- Jev 후보와 조합: 실제로 나가는 본문 ----------------------------------------------------


class RecordingTransport:
    def __init__(self):
        self.bodies = []

    def send(self, request):
        self.bodies.append(request.body)
        others = [c for c in CHOICES if c != "clear"]
        answer = {
            "type": "choice",
            "choice": "clear",
            "probabilities": {"clear": 0.9, others[0]: 0.1, others[1]: 0},
        }
        return DecisionsReply(200, json.dumps({"answers": {QUESTION: answer}}).encode())


def test_jev_wire_body_is_canonical_only_CREATE_US_08_AC15():
    transport = RecordingTransport()
    guard = ComposedSessionSafetyGuard(Recorder(), JevStorySafetyGuard(transport))
    assert guard(SESSION) is CommonSafety.CLEAR
    [body] = transport.bodies
    assert body == build_request(CANONICAL).body
    text = body.decode("utf-8")
    assert not any(value in text for value in SESSION_VALUES)
    assert set(json.loads(text)["state"]) == {
        "title",
        "subtitle",
        "world_description",
        "tags",
        "characters",
        "relations",
        "intros",
        "creator_presets",
    }


def test_plain_jev_guard_still_closes_on_session_surface_CREATE_US_08_AC15():
    """공개 판정기 의미는 그대로다: Jev 후보만 주입하면 세션 표면은 보내지 않고 닫힌다."""
    transport = RecordingTransport()
    applied_only = dataclasses.replace(
        CANONICAL, bound_applied_profile=SafetyProfile(APPLIED_NAME, APPLIED_DESC)
    )
    assert JevStorySafetyGuard(transport)(SESSION) is CommonSafety.UNAVAILABLE
    assert JevStorySafetyGuard(transport)(applied_only) is CommonSafety.UNAVAILABLE
    assert transport.bodies == []
