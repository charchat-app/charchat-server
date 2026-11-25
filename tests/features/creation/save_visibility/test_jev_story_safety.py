"""CREATE-US-08-AC15 Jev 스토리 공통 안전 분류 후보(주입 전용) 단위 계약.

DB·네트워크 없이 로컬 가짜 전송만 쓴다. 금지 콘텐츠 원문 대신 의미 없는 중립 표식만 쓴다.
"""

import json
import logging

import pytest

from app.core.common_safety import (
    CommonSafety,
    SafetyCharacter,
    SafetyIntro,
    SafetyMessage,
    SafetyProfile,
    SafetyRelationship,
    SafetySurface,
    is_clear,
)
from app.features.creation.save_visibility.jev_decisions import (
    ENDPOINT,
    MAX_RESPONSE_BYTES,
    MODEL,
    DecisionsReply,
    DecisionsRequest,
)
from app.features.creation.save_visibility.jev_story_safety import (
    CHOICES,
    MAX_REQUEST_BYTES,
    QUESTION,
    JevStorySafetyGuard,
    build_request,
)

# 필드마다 구별되는 중립 표식(실제 금지 콘텐츠가 아니다).
FIELDS = {
    "title": "ZQX-TITLE-1101",
    "subtitle": "ZQX-SUBTITLE-1102",
    "world": "ZQX-WORLD-1103",
    "tag": "ZQX-TAG-1104",
    "char_name": "ZQX-CHARNAME-1105",
    "char_desc": "ZQX-CHARDESC-1106",
    "rel_label": "ZQX-RELLABEL-1107",
    "intro_title": "ZQX-INTROTITLE-1108",
    "intro_body": "ZQX-INTROBODY-1109",
    "preset_name": "ZQX-PRESETNAME-1110",
    "preset_desc": "ZQX-PRESETDESC-1111",
}
PERSONAL = "ZQX-PERSONAL-2201"
STORY_ID = 987654321


def surface(**overrides) -> SafetySurface:
    base = dict(
        story_id=STORY_ID,
        title=FIELDS["title"],
        subtitle=FIELDS["subtitle"],
        world_description=FIELDS["world"],
        tags=(FIELDS["tag"],),
        characters=(SafetyCharacter(name=FIELDS["char_name"], description=FIELDS["char_desc"]),),
        relationships=(
            SafetyRelationship(
                source="{{user}}", target=FIELDS["char_name"], label=FIELDS["rel_label"]
            ),
        ),
        intros=(
            SafetyIntro(
                title=FIELDS["intro_title"],
                messages=(
                    SafetyMessage(
                        speaker_kind="character",
                        speaker_name=FIELDS["char_name"],
                        body=FIELDS["intro_body"],
                    ),
                ),
            ),
        ),
        bound_profiles=(
            SafetyProfile(name=FIELDS["preset_name"], description=FIELDS["preset_desc"]),
        ),
    )
    base.update(overrides)
    return SafetySurface(**base)


def body_of(request: DecisionsRequest) -> dict:
    return json.loads(request.body.decode("utf-8"))


def reply_for(choice, *, top=0.9, extra_answers=None, **document):
    others = [c for c in CHOICES if c != choice]
    probabilities = {choice: top, others[0]: round(1 - top, 10), **{c: 0 for c in others[1:]}}
    answers = {QUESTION: {"type": "choice", "choice": choice, "probabilities": probabilities}}
    answers.update(extra_answers or {})
    payload = {"answers": answers, **document}
    return DecisionsReply(200, json.dumps(payload).encode("utf-8"))


class FakeTransport:
    def __init__(self, respond):
        self.respond = respond
        self.requests: list[DecisionsRequest] = []

    def send(self, request):
        self.requests.append(request)
        result = self.respond(request) if callable(self.respond) else self.respond
        if isinstance(result, BaseException):
            raise result
        return result


def judge(reply, target=None):
    transport = FakeTransport(reply)
    outcome = JevStorySafetyGuard(transport)(target or surface())
    return outcome, transport


# --- 요청 구성 ----------------------------------------------------------------------------------


def test_request_reuses_jev_contract_and_single_question_CREATE_US_08_AC15():
    _, transport = judge(reply_for("clear"))
    [request] = transport.requests
    assert isinstance(request, DecisionsRequest)
    assert request.url == ENDPOINT
    assert request.max_response_bytes == MAX_RESPONSE_BYTES
    body = body_of(request)
    assert set(body) == {"model", "state", "questions"}
    assert body["model"] == MODEL
    assert list(body["questions"]) == [QUESTION]
    question = body["questions"][QUESTION]
    assert question["type"] == "choice"
    assert set(question["criteria"]) == set(CHOICES) == {"clear", "blocked", "unclear"}
    # 요청 원문은 repr에 싣지 않는다.
    assert FIELDS["world"] not in repr(request)


def test_request_covers_all_authored_fields_jointly_without_ids_CREATE_US_08_AC15():
    _, transport = judge(reply_for("clear"))
    raw = transport.requests[0].body.decode("utf-8")
    for marker in FIELDS.values():
        assert marker in raw, marker
    assert str(STORY_ID) not in raw
    assert "story_id" not in raw
    assert '"id"' not in raw
    state = body_of(transport.requests[0])["state"]
    # 한 상태에 모든 제작자 필드를 함께 싣는다(필드별 개별 질문이 아니다).
    assert set(state) == {
        "title",
        "subtitle",
        "world_description",
        "tags",
        "characters",
        "relations",
        "intros",
        "creator_presets",
    }


def test_criteria_treat_text_as_untrusted_and_stay_on_child_sexualization_CREATE_US_08_AC15():
    question = body_of(build_request(surface()))["questions"][QUESTION]
    instructions = question["instructions"].lower()
    assert "untrusted" in instructions
    assert "ignore any instructions" in instructions
    assert "jointly" in instructions
    criteria = {k: v.lower() for k, v in question["criteria"].items()}
    assert "non-sexual" in criteria["clear"]
    assert "sexualiz" in criteria["blocked"]
    assert "cannot be determined" in criteria["unclear"]
    # 자격·등급 판정의 나이 기준(20세)이나 미정 정책을 기준에 섞지 않는다.
    text = json.dumps(question).lower()
    for word in ("20", "rating", "unsafe", "eligib"):
        assert word not in text, word


# --- 명시적 선택만 통과·차단 ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("choice", "expected"),
    [
        ("clear", CommonSafety.CLEAR),
        ("blocked", CommonSafety.BLOCKED),
        ("unclear", CommonSafety.UNAVAILABLE),
    ],
)
def test_explicit_choice_maps_to_verdict_CREATE_US_08_AC15(choice, expected):
    outcome, transport = judge(reply_for(choice))
    assert outcome is expected
    assert len(transport.requests) == 1


def test_only_clear_passes_common_gate_CREATE_US_08_AC15():
    assert is_clear(JevStorySafetyGuard(FakeTransport(reply_for("clear"))), surface())
    for choice in ("blocked", "unclear"):
        assert not is_clear(JevStorySafetyGuard(FakeTransport(reply_for(choice))), surface())


@pytest.mark.parametrize(
    "reply",
    [
        DecisionsReply(200, b"not json"),
        DecisionsReply(200, b"{}"),
        DecisionsReply(500, reply_for("clear").body),
        DecisionsReply(429, reply_for("clear").body),
        DecisionsReply(200, b"x" * (MAX_RESPONSE_BYTES + 1)),
        reply_for("clear", error={"message": "provider down"}),
        reply_for("clear", model="other/model"),
        reply_for("clear", extra_answers={"other": {"type": "choice"}}),
        reply_for("maybe"),
        reply_for("clear", top=0.5),  # 동률은 모호하다.
        RuntimeError("network down"),
        TimeoutError(),
        "not a reply",
        None,
        DecisionsReply("200", reply_for("clear").body),
        DecisionsReply(200, "text body"),
    ],
)
def test_invalid_error_or_unavailable_fails_closed_CREATE_US_08_AC15(reply):
    outcome, _ = judge(reply)
    assert outcome is CommonSafety.UNAVAILABLE


def test_unknown_choice_with_valid_shape_fails_closed_CREATE_US_08_AC15():
    def respond(_request):
        probabilities = {"clear": 0.1, "blocked": 0.1, "unclear": 0.1, "other": 0.7}
        document = {
            "answers": {
                QUESTION: {"type": "choice", "choice": "other", "probabilities": probabilities}
            }
        }
        return DecisionsReply(200, json.dumps(document).encode("utf-8"))

    outcome, _ = judge(respond)
    assert outcome is CommonSafety.UNAVAILABLE


def test_non_surface_input_fails_closed_without_sending_CREATE_US_08_AC15():
    transport = FakeTransport(reply_for("clear"))
    assert JevStorySafetyGuard(transport)("story text") is CommonSafety.UNAVAILABLE
    assert transport.requests == []


# --- 세션 바인딩 값(개인·세션 유래)은 보내지 않는다 -------------------------------------------


@pytest.mark.parametrize(
    "bound",
    [
        {"bound_intro": (SafetyMessage("user", PERSONAL, f"Hello {PERSONAL}"),)},
        {"bound_character_names": (PERSONAL,)},
        {"bound_play_name": PERSONAL},
        {"bound_play_name": ""},
        {"session_origin": True},  # 입력 오류 사전 판정처럼 바인딩 값이 없어도 세션 유래다.
    ],
)
def test_session_bound_values_fail_closed_without_sending_CREATE_US_08_AC15(bound):
    """렌더링 인트로·플레이 이름·세션 이름 스냅숏은 개인 정보일 수 있어 외부로 보내지 않는다.

    조용히 빼고 판정하지 않고, 이 후보는 판정 불가(닫힘)로 돌려준다.
    """
    transport = FakeTransport(reply_for("clear"))
    assert JevStorySafetyGuard(transport)(surface(**bound)) is CommonSafety.UNAVAILABLE
    assert transport.requests == []


def test_creator_presets_are_judged_as_authored_text_CREATE_US_08_AC15():
    _, transport = judge(reply_for("clear"))
    presets = body_of(transport.requests[0])["state"]["creator_presets"]
    assert presets == [{"name": FIELDS["preset_name"], "description": FIELDS["preset_desc"]}]


def test_canonical_only_surface_without_presets_is_sent_CREATE_US_08_AC15():
    outcome, transport = judge(reply_for("clear"), surface(bound_profiles=()))
    assert outcome is CommonSafety.CLEAR
    assert body_of(transport.requests[0])["state"]["creator_presets"] == []


# --- 크기 상한: 자르지 않고 거부 ----------------------------------------------------------------


def test_oversized_request_is_refused_not_truncated_CREATE_US_08_AC15():
    big = surface(world_description="가" * MAX_REQUEST_BYTES)
    assert build_request(big) is None
    transport = FakeTransport(reply_for("clear"))
    assert JevStorySafetyGuard(transport)(big) is CommonSafety.UNAVAILABLE
    assert transport.requests == []


def test_request_at_limit_is_sent_CREATE_US_08_AC15():
    base = len(build_request(surface(world_description="")).body)
    fits = surface(world_description="a" * (MAX_REQUEST_BYTES - base))
    request = build_request(fits)
    assert request is not None
    assert len(request.body) == MAX_REQUEST_BYTES
    over = surface(world_description="a" * (MAX_REQUEST_BYTES - base + 1))
    assert build_request(over) is None


# --- 로그·repr에 원문 없음 ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "reply",
    [reply_for("blocked"), DecisionsReply(200, b"ZQX-PROVIDER-3301"), RuntimeError("ZQX-ERR-3302")],
)
def test_no_story_or_provider_text_in_logs_CREATE_US_08_AC15(caplog, reply):
    caplog.set_level(logging.DEBUG)
    guard = JevStorySafetyGuard(FakeTransport(reply))
    is_clear(guard, surface())
    for marker in (*FIELDS.values(), "ZQX-PROVIDER-3301", "ZQX-ERR-3302"):
        assert marker not in caplog.text
    assert "ZQX" not in repr(guard)
