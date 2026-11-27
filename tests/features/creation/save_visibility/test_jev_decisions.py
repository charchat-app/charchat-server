"""CREATE-US-08-AC04~AC08 Jev Decisions 평가기 어댑터 계약(DB·네트워크 불필요).

전송은 가짜 객체로 대체한다. 요청 구성(로컬 ID·질문별 허용 선택지)과 닫힌 실패 파서를 본다.
고정값은 중립 합성값만 쓴다.
"""

import json

import pytest

from app.features.creation.save_visibility.jev_decisions import (
    COMPATIBLE_MODELS,
    ENDPOINT,
    MAX_RESPONSE_BYTES,
    MODEL,
    DecisionsReply,
    DecisionsRequest,
    JevDecisionsEvaluator,
    build_plan,
)
from app.features.creation.save_visibility.rating_review import (
    RatingReviewCoordinator,
    build_snapshot,
    review_result,
)
from app.features.creation.save_visibility.review_verdict import (
    Eligibility,
    EligibilityReason,
    EligibilityVerdict,
    unavailable,
)

R = EligibilityReason
SECRET = "sk-or-must-not-leak"
DEFAULTS = {
    "age_band": "at_least_20",
    "age_context": "adult",
    "minor_portrayal": "absent",
    "kinship_in_description": "none",
    "kinship_in_relation": "none",
    "user_character_age": "no_age_evidence",
}
# 실제 정규화 개정(캐시된 Jev 응답의 `model`)과 공급자 표기.
REVISION = "typesafe/jev-1.14-20261014"


def verdict(outcome, *reasons):
    return EligibilityVerdict(outcome=outcome, reasons=frozenset(reasons))


def snap(ids=(9001, 9002), relations=(), world="세계"):
    people = [{"id": i, "name": f"인물{n}", "description": f"상세{n}"} for n, i in enumerate(ids)]
    rows = [
        {
            "source_kind": s_kind,
            "source_character_id": s_id,
            "target_kind": t_kind,
            "target_character_id": t_id,
            "label": label,
        }
        for s_kind, s_id, t_kind, t_id, label in relations
    ]
    return build_snapshot(world, people, rows)


def body_of(request):
    return json.loads(request.body.decode("utf-8"))


def valid_answers(request, **overrides):
    """요청 질문마다 기본 선택(적격)을 고른 응답 답. 질문 이름 접미사로 기본값을 정한다."""
    answers = {}
    for name in body_of(request)["questions"]:
        suffix = next(k for k in DEFAULTS if name.endswith(k))
        choice = overrides.get(name, DEFAULTS[suffix])
        answers[name] = {
            "type": "choice",
            "choice": choice,
            "probabilities": full_probabilities(request, name, choice),
        }
    return answers


def full_probabilities(request, name, choice, top=0.9):
    """실제 응답처럼 모든 선택지를 담고 합이 정확히 1인 분포. 고른 값이 유일한 최댓값이다."""
    others = [c for c in body_of(request)["questions"][name]["criteria"] if c != choice]
    return {choice: top, others[0]: round(1 - top, 10), **{c: 0 for c in others[1:]}}


def encode(document):
    return json.dumps(document, ensure_ascii=False).encode("utf-8")


class FakeTransport:
    """요청을 기록하고 `respond(request)`가 돌려준 응답 또는 예외를 낸다."""

    def __init__(self, respond=None, **overrides):
        self.requests = []
        self.respond = respond or (
            lambda request: DecisionsReply(
                200, encode({"answers": valid_answers(request, **overrides)})
            )
        )

    def send(self, request):
        self.requests.append(request)
        result = self.respond(request)
        if isinstance(result, BaseException):
            raise result
        return result


def evaluate(snapshot=None, transport=None, **overrides):
    transport = transport or FakeTransport(**overrides)
    result = JevDecisionsEvaluator(transport).evaluate(77, snapshot or snap())
    return result, transport


# --- 요청 구성 -----------------------------------------------------------------------------------


def test_request_uses_fixed_endpoint_model_and_bounded_reply_CREATE_US_08_AC04():
    _, transport = evaluate()
    [request] = transport.requests
    assert isinstance(request, DecisionsRequest)
    assert request.url == ENDPOINT == "https://openrouter.ai/api/alpha/decisions"
    assert body_of(request)["model"] == MODEL == "typesafe/jev-1.14"
    assert request.max_response_bytes == MAX_RESPONSE_BYTES
    assert set(body_of(request)) == {"model", "state", "questions"}
    # 요청 원문은 repr에 싣지 않는다.
    assert "인물" not in repr(request)


def test_state_has_only_normalized_snapshot_with_local_ids_CREATE_US_08_AC04_AC06():
    relations = [
        ("character", 9001, "character", 9002, "형제"),
        ("user", None, "character", 9002, "친구"),
    ]
    _, transport = evaluate(snap(relations=relations))
    state = body_of(transport.requests[0])["state"]
    assert state == {
        "world_description": "세계",
        "characters": [
            {"id": "c1", "name": "인물0", "description": "상세0"},
            {"id": "c2", "name": "인물1", "description": "상세1"},
        ],
        "relations": [
            {"id": "r1", "source": "c1", "target": "c2", "label": "형제"},
            {"id": "r2", "source": "user", "target": "c2", "label": "친구"},
        ],
    }
    raw = transport.requests[0].body.decode("utf-8")
    for db_id in ("9001", "9002", "77"):
        assert db_id not in raw  # DB ID·스토리 ID를 보내지 않는다.


def test_each_roster_member_gets_separate_strict_choice_questions_CREATE_US_08_AC04_AC08():
    relations = [
        ("character", 9002, "user", None, "부녀"),
        ("character", 9001, "character", 9002, "남매"),  # 명단끼리 관계는 묻지 않는다.
    ]
    _, transport = evaluate(snap(relations=relations))
    questions = body_of(transport.requests[0])["questions"]
    assert set(questions) == {
        "c1_age_band",
        "c1_age_context",
        "c1_minor_portrayal",
        "c1_kinship_in_description",
        "c2_age_band",
        "c2_age_context",
        "c2_minor_portrayal",
        "c2_kinship_in_description",
        "c2_kinship_in_relation",  # c1에는 유저와의 서면 관계가 없다.
        "user_character_age",
    }
    for question in questions.values():
        assert question["type"] == "choice"
        assert "data" in question["instructions"]  # 상태는 데이터로만 다룬다.
    assert set(questions["c1_age_band"]["criteria"]) == {"under_20", "at_least_20", "unstated"}
    assert set(questions["c1_age_context"]["criteria"]) == {"adult", "none", "contradictory"}
    assert set(questions["c1_minor_portrayal"]["criteria"]) == {"absent", "depicted", "unclear"}
    kin = {"none", "non_restricted", "biological_parent_child", "biological_sibling", "unclear"}
    assert set(questions["c1_kinship_in_description"]["criteria"]) == kin
    assert set(questions["c2_kinship_in_relation"]["criteria"]) == kin
    # 정렬된 관계 r1=명단끼리(c1↔c2), r2=c2↔유저. c2 관계 질문에는 r2만 싣는다.
    assert "r2" in questions["c2_kinship_in_relation"]["instructions"]
    assert "r1" not in questions["c2_kinship_in_relation"]["instructions"]
    assert set(questions["user_character_age"]["criteria"]) == {
        "no_age_evidence",
        "adult",
        "under_20",
        "minor",
        "unclear",
    }


def test_kinship_criteria_are_only_counterpart_and_user_biological_ties_CREATE_US_08_AC06_AC08():
    """승인 범위: 명단 상대역과 유저 캐릭터 사이의 생물학적 혈연만 제한 혈연이다."""
    _, transport = evaluate(snap(relations=[("user", None, "character", 9001, "친구")]))
    questions = body_of(transport.requests[0])["questions"]
    for name in ("c1_kinship_in_description", "c1_kinship_in_relation"):
        criteria = questions[name]["criteria"]
        for restricted in ("biological_parent_child", "biological_sibling"):
            assert "the user" in criteria[restricted]
            assert "another roster character" not in criteria[restricted]
        non_restricted = criteria["non_restricted"]
        for word in ("step", "adoptive", "nickname", "other roster characters"):
            assert word in non_restricted
        assert "guess" in criteria["unclear"]
    description = questions["c1_kinship_in_description"]["instructions"]
    assert "Ignore ties between roster characters" in description
    assert "relations list" in description


def test_kinship_criteria_cover_short_korean_terms_and_honorifics_CREATE_US_08_AC06_AC08():
    """합성 사례 회귀: 관계만 있는 `친형제자매`는 형제자매, 호칭만 쓴 `오빠`는 비제한 관계다.

    판정 정책은 그대로 두고 질문 문구만 본다. 명시적 짧은 한국어 친족어는 추가 문장 없이 혈연을
    정하고, 뜻이 갈리는 맨 형제 용어는 추측하지 않으며, 출처 범위 제한은 유지한다.
    """
    _, transport = evaluate(snap(relations=[("user", None, "character", 9001, "친형제자매")]))
    questions = body_of(transport.requests[0])["questions"]
    for name in ("c1_kinship_in_description", "c1_kinship_in_relation"):
        criteria = questions[name]["criteria"]
        sibling = criteria["biological_sibling"]
        assert "친형제자매" in sibling
        assert "no extra sentence" in sibling  # 명시적 친족어만으로 충분하다.
        unclear = criteria["unclear"]
        assert "형제" in unclear and "do not guess" in unclear  # 맨 형제 용어는 추측 금지.
        assert "친형제자매" not in unclear
        non_restricted = criteria["non_restricted"]
        assert "오빠" in non_restricted and "honorific" in non_restricted
        assert "only an honorific is non_restricted, not none" in criteria["none"]
    # 출처 범위 제한은 그대로다.
    assert "ignore the relations list" in questions["c1_kinship_in_description"]["instructions"]
    relation = questions["c1_kinship_in_relation"]["instructions"]
    assert "only in these relations: r1." in relation
    assert "Ignore descriptions and other relations." in relation


def test_minor_portrayal_excludes_numeric_age_alone_keeps_separate_cues_CREATE_US_08_AC04_AC09():
    """미성년 묘사 질문: 숫자 나이만으로는 독립 묘사 근거가 아니다(나이는 age_band가 본다).

    실제로 따로 적힌 근거(외모·학년·역할·행동)는 명시 나이와 관계없이 그대로 묘사로 본다.
    선택지 집합·질문 이름·판정 매핑은 바꾸지 않는다.
    """
    _, transport = evaluate()
    questions = body_of(transport.requests[0])["questions"]
    for key in ("c1", "c2"):
        question = questions[f"{key}_minor_portrayal"]
        assert set(question["criteria"]) == {"absent", "depicted", "unclear"}
        depicted = question["criteria"]["depicted"]
        # 따로 적힌 근거는 명시 나이와 관계없이 유지한다.
        for cue in ("appearance", "school grade", "role", "behavior"):
            assert cue in depicted
        assert "regardless of any stated age" in depicted
        # 숫자 나이만으로는 독립 근거가 아님을 질문 계약에 명시한다.
        contract = " ".join([question["instructions"], *question["criteria"].values()]).lower()
        sentences = [s for s in contract.split(".") if "numeric age by itself" in s]
        assert sentences, "numeric age by itself must be explicitly addressed"
        assert any(" not " in f" {s} " for s in sentences)
        assert any("evidence" in s for s in sentences)


def test_user_character_question_is_narrow_independent_depiction_CREATE_US_08_AC04():
    _, transport = evaluate()
    question = body_of(transport.requests[0])["questions"]["user_character_age"]
    text = question["instructions"]
    assert "user's own character" in text
    assert "not a depiction" in text  # 관계로 이어진 것만으로는 묘사가 아니다.
    assert "Ignore other people" in text  # 우연히 나오는 제3자는 대상이 아니다.
    assert "under 20" in question["criteria"]["under_20"]


def test_plan_repr_hides_internal_id_maps_CREATE_US_08_AC04():
    plan = build_plan(snap())
    assert "9001" not in repr(plan) and "c1" not in repr(plan)


@pytest.mark.parametrize(
    "relations",
    [
        [("character", 1234, "user", None, "부녀")],  # 명단 밖 인물
        [("npc", None, "user", None, "부녀")],  # 알 수 없는 종류
    ],
)
def test_malformed_snapshot_is_not_sent_CREATE_US_08_AC04(relations):
    result, transport = evaluate(snap(relations=relations))
    assert result == unavailable(R.INVALID_SIGNALS)
    assert transport.requests == []


def test_empty_roster_is_not_sent_CREATE_US_08_AC04():
    result, transport = evaluate(snap(ids=()))
    assert result == unavailable(R.INVALID_SIGNALS)
    assert transport.requests == []


# --- 판정 매핑(AC04~AC08) ------------------------------------------------------------------------


def test_adult_roster_without_relations_is_eligible_CREATE_US_08_AC05_AC08():
    result, _ = evaluate()
    assert result == verdict(Eligibility.ELIGIBLE)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"c2_age_band": "under_20"}, verdict(Eligibility.INELIGIBLE, R.UNDERAGE_STATED)),
        (
            {"c1_minor_portrayal": "depicted"},
            verdict(Eligibility.INELIGIBLE, R.MINOR_DEPICTED),
        ),
        (
            {"c1_age_band": "unstated", "c1_age_context": "none"},
            verdict(Eligibility.UNDETERMINED, R.AGE_UNDETERMINED),
        ),
        ({"c1_age_band": "unstated"}, verdict(Eligibility.ELIGIBLE)),  # 성인 맥락(AC05)
        (
            {"c1_kinship_in_description": "biological_sibling"},
            verdict(Eligibility.INELIGIBLE, R.RESTRICTED_KINSHIP),
        ),
        ({"c1_kinship_in_description": "non_restricted"}, verdict(Eligibility.ELIGIBLE)),
        (
            {"c2_kinship_in_description": "unclear"},
            verdict(Eligibility.UNDETERMINED, R.KINSHIP_UNDETERMINED),
        ),
    ],
)
def test_choices_map_to_existing_verdict_rules_CREATE_US_08_AC04_AC08(overrides, expected):
    result, _ = evaluate(**overrides)
    assert result == expected


@pytest.mark.parametrize("direction", ["user-first", "character-first"])
def test_user_counterpart_relation_kinship_is_asked_CREATE_US_08_AC06_AC07(direction):
    """R2: 초기 관계에 유저와의 친자녀 관계가 적혀 있으면 부적격이다."""
    ends = [("user", None), ("character", 9002)]
    if direction == "character-first":
        ends.reverse()
    (s_kind, s_id), (t_kind, t_id) = ends
    relations = [(s_kind, s_id, t_kind, t_id, "친자녀")]
    result, _ = evaluate(
        snap(relations=relations), c2_kinship_in_relation="biological_parent_child"
    )
    assert result == verdict(Eligibility.INELIGIBLE, R.RESTRICTED_KINSHIP)


def test_roster_to_roster_relation_is_not_asked_and_not_disqualifying_CREATE_US_08_AC06_AC08():
    """명단 인물 둘 사이 혈연은 승인 범위(상대역↔유저)가 아니다. 묻지 않고 none이다."""
    relations = [("character", 9001, "character", 9002, "친남매")]
    result, transport = evaluate(snap(relations=relations))
    assert result == verdict(Eligibility.ELIGIBLE)
    questions = body_of(transport.requests[0])["questions"]
    assert not [name for name in questions if name.endswith("_kinship_in_relation")]


def test_no_applicable_relation_is_deterministically_none_CREATE_US_08_AC06_AC08():
    """유저와의 서면 관계가 없으면 묻지 않고 none이다. 응답에 그 답이 끼어 있으면 거부한다."""

    def respond(request):
        answers = valid_answers(request)
        answers["c1_kinship_in_relation"] = {
            "type": "choice",
            "choice": "biological_sibling",
            "probabilities": {"biological_sibling": 1.0},
        }
        return DecisionsReply(200, encode({"answers": answers}))

    assert evaluate()[0] == verdict(Eligibility.ELIGIBLE)
    assert evaluate(transport=FakeTransport(respond))[0] == unavailable(R.INVALID_SIGNALS)


@pytest.mark.parametrize(
    ("choice", "expected"),
    [
        # A6: 세계관에 17세 유저 캐릭터 → 부적격.
        ("under_20", verdict(Eligibility.INELIGIBLE, R.UNDERAGE_STATED)),
        ("minor", verdict(Eligibility.INELIGIBLE, R.MINOR_DEPICTED)),
        ("unclear", verdict(Eligibility.UNDETERMINED, R.AGE_UNDETERMINED)),
        ("adult", verdict(Eligibility.ELIGIBLE)),
        # 유저 캐릭터를 독립적으로 묘사하지 않음(관계로 이어진 것만 있음) → 추가 사유 없음.
        ("no_age_evidence", verdict(Eligibility.ELIGIBLE)),
    ],
)
def test_user_character_age_is_checked_separately_CREATE_US_08_AC04(choice, expected):
    result, _ = evaluate(user_character_age=choice)
    assert result == expected


def test_user_character_unclear_keeps_confirmed_roster_reason_CREATE_US_08_AC04():
    result, _ = evaluate(
        user_character_age="unclear", c1_kinship_in_description="biological_sibling"
    )
    assert result == verdict(Eligibility.INELIGIBLE, R.RESTRICTED_KINSHIP, R.AGE_UNDETERMINED)


# --- 닫힌 실패 파서 ------------------------------------------------------------------------------


def _reply(document_or_bytes, status=200):
    raw = document_or_bytes
    if not isinstance(raw, bytes):
        raw = encode(raw)
    return lambda request: DecisionsReply(status, raw)


def _mutate(change):
    def respond(request):
        document = {"answers": valid_answers(request)}
        change(document)
        return DecisionsReply(200, encode(document))

    return respond


def _set_answer(key, value):
    return _mutate(lambda d: d["answers"]["c1_age_band"].__setitem__(key, value))


def _probs(at_least_20, under_20=0, unstated=0):
    """c1_age_band(선택 at_least_20)의 전체 확률 분포를 바꾼다."""
    values = {"at_least_20": at_least_20, "under_20": under_20, "unstated": unstated}
    return _set_answer("probabilities", values)


def _raw_number(key, literal):
    """JSON 숫자 리터럴을 그대로 넣는다(파이썬 float로 만들 수 없는 값)."""

    def respond(request):
        document = {"answers": valid_answers(request)}
        document["answers"]["c1_age_band"][key] = "@@"
        return DecisionsReply(200, encode(document).replace(b'"@@"', literal))

    return respond


INVALID = {
    "not-json": _reply(b"{not json"),
    "not-utf8": _reply(b"\xff\xfe"),
    "not-object": _reply([]),
    "no-answers": _reply({}),
    "nan": _reply(b'{"answers": NaN}'),
    "unexpected-top-key": _mutate(lambda d: d.__setitem__("verdict", "eligible")),
    "missing-answer": _mutate(lambda d: d["answers"].pop("c1_age_band")),
    "extra-answer": _mutate(lambda d: d["answers"].__setitem__("c9_age_band", {})),
    "answer-not-object": _mutate(lambda d: d["answers"].__setitem__("c1_age_band", "under_20")),
    "unknown-choice": _set_answer("choice", "eighteen"),
    "choice-from-other-question": _set_answer("choice", "adult"),
    "choice-not-string": _set_answer("choice", 20),
    "choice-unhashable": _set_answer("choice", ["at_least_20"]),
    "wrong-type": _set_answer("type", "number"),
    "type-unhashable": _set_answer("type", {"kind": "choice"}),
    "type-null": _set_answer("type", None),
    "answers-not-object": _mutate(lambda d: d.__setitem__("answers", [])),
    "usage-not-object": _mutate(lambda d: d.__setitem__("usage", 3)),
    "document-not-object": lambda request: DecisionsReply(200, b"[1, 2]"),
    "extra-answer-key": _set_answer("reason", "x"),
    "missing-probabilities": _mutate(lambda d: d["answers"]["c1_age_band"].pop("probabilities")),
    "probabilities-not-object": _set_answer("probabilities", [0.9]),
    "probability-unknown-choice": _set_answer("probabilities", {"eighteen": 0.9}),
    "probability-partial-map": _set_answer("probabilities", {"at_least_20": 1.0}),
    "probability-extra-choice": _set_answer(
        "probabilities", {"at_least_20": 1, "under_20": 0, "unstated": 0, "eighteen": 0}
    ),
    "probability-bool": _probs(True),
    "probability-string": _probs("1"),
    "probability-null": _probs(None),
    "probability-out-of-range": _probs(1.5, under_20=-0.5),
    "probability-huge-int": _probs(10**400, under_20=-(10**400) + 1),
    "probability-float-overflow": _raw_number("probabilities", b'{"at_least_20": 1e400}'),
    "probability-sum-low": _probs(0.5, under_20=0.2, unstated=0.2),
    "probability-sum-high": _probs(0.9, under_20=0.2),
    "choice-contradicted": _probs(0.2, under_20=0.8),
    "choice-tied": _probs(0.5, under_20=0.5),  # 동률은 모호함 → 거부
    "confidence-bool": _set_answer("confidence", True),
    "confidence-string": _set_answer("confidence", "0.9"),
    "confidence-null": _set_answer("confidence", None),
    "confidence-out-of-range": _set_answer("confidence", 1.5),
    "confidence-negative": _set_answer("confidence", -0.1),
    "confidence-huge-int": _set_answer("confidence", 10**400),
    "confidence-float-overflow": _raw_number("confidence", b"1e400"),
    "model-mismatch": _mutate(lambda d: d.__setitem__("model", "other/model-2")),
    "model-unknown-revision": _mutate(lambda d: d.__setitem__("model", MODEL + "-20991231")),
    "model-next-version": _mutate(lambda d: d.__setitem__("model", "typesafe/jev-1.15")),
    "model-unhashable-object": _mutate(lambda d: d.__setitem__("model", {"id": MODEL})),
    "model-unhashable-list": _mutate(lambda d: d.__setitem__("model", [MODEL])),
    "model-null": _mutate(lambda d: d.__setitem__("model", None)),
    "provider-not-string": _mutate(lambda d: d.__setitem__("provider", ["TypeSafe"])),
    "provider-null": _mutate(lambda d: d.__setitem__("provider", None)),
    "id-not-string": _mutate(lambda d: d.__setitem__("id", 1)),
    "duplicate-top-key": lambda request: DecisionsReply(
        200,
        b'{"answers": {}, "answers": ' + encode(valid_answers(request)) + b"}",
    ),
    "duplicate-answer-key": lambda request: DecisionsReply(
        200,
        encode({"answers": valid_answers(request)})[:-2]
        + b', "c1_age_band": {"type": "choice", "choice": "under_20", "probabilities": {}}}}',
    ),
    "oversized": lambda request: DecisionsReply(200, b" " * (request.max_response_bytes + 1)),
    "status-bool": lambda request: DecisionsReply(
        True, encode({"answers": valid_answers(request)})
    ),
    "body-str": lambda request: DecisionsReply(
        200, json.dumps({"answers": valid_answers(request)})
    ),
    "reply-wrong-type": lambda request: {"status": 200, "body": b"{}"},
}


@pytest.mark.parametrize("respond", INVALID.values(), ids=INVALID.keys())
def test_malformed_or_ambiguous_reply_is_rejected_CREATE_US_08_AC04(respond):
    transport = FakeTransport(respond)
    result, _ = evaluate(transport=transport)
    assert result == unavailable(R.INVALID_SIGNALS)
    assert len(transport.requests) == 1  # 자동 재시도는 없다.


FAILURES = {
    "status-500": _reply({"answers": {}}, status=500),
    "status-429": _reply(b"", status=429),
    "status-201": lambda request: DecisionsReply(201, encode({"answers": valid_answers(request)})),
    "provider-error": _mutate(lambda d: d.__setitem__("error", {"message": "rate limited"})),
    "transport-error": lambda request: OSError("connection reset"),
    "transport-timeout": lambda request: TimeoutError(),
}


@pytest.mark.parametrize("respond", FAILURES.values(), ids=FAILURES.keys())
def test_provider_or_transport_failure_is_outage_CREATE_US_08_AC04(respond):
    transport = FakeTransport(respond)
    result, _ = evaluate(transport=transport)
    assert result == unavailable(R.EVALUATOR_FAILURE)
    assert len(transport.requests) == 1


def test_only_known_alias_and_revision_are_compatible_CREATE_US_08_AC04():
    assert COMPATIBLE_MODELS == {MODEL, REVISION}


@pytest.mark.parametrize("model", [MODEL, REVISION])
def test_actual_response_fields_are_accepted_CREATE_US_08_AC04(model):
    """캐시된 실제 응답 형태: 정규화 개정 `model`, 최상위 `provider`, 답별 선택적 `confidence`."""

    def respond(request):
        answers = valid_answers(request)
        answers["c1_age_band"]["confidence"] = 0.97
        answers["c2_age_band"]["confidence"] = 1  # 정수 1도 숫자다.
        document = {
            "id": "gen-1",
            "model": model,
            "provider": "TypeSafe",
            "error": None,
            "usage": {"cost": 0.000042336},
            "answers": answers,
        }
        return DecisionsReply(200, encode(document))

    assert evaluate(transport=FakeTransport(respond))[0] == verdict(Eligibility.ELIGIBLE)


def test_cached_jev_probability_shape_is_accepted_CREATE_US_08_AC04():
    """실제 Jev 분포처럼 0과 소수가 섞이고 합이 정확히 1이면 받는다(예: 0.86/0.11/0.03)."""
    result, _ = evaluate(
        transport=FakeTransport(lambda request: _probs(0.86, under_20=0.11, unstated=0.03)(request))
    )
    assert result == verdict(Eligibility.ELIGIBLE)


def test_low_probability_or_confidence_does_not_change_choice_CREATE_US_08_AC04():
    """확률·확신도 임계값을 두지 않는다. 유일한 최댓값인 선택지를 그대로 쓴다."""

    def respond(request):
        answers = valid_answers(request)
        answers["c1_age_band"]["probabilities"] = {
            "at_least_20": 0.34,
            "under_20": 0.33,
            "unstated": 0.33,
        }
        answers["c1_age_band"]["confidence"] = 0.01
        return DecisionsReply(200, encode({"answers": answers}))

    assert evaluate(transport=FakeTransport(respond))[0] == verdict(Eligibility.ELIGIBLE)


def test_adapter_keeps_no_provider_text_or_credentials_CREATE_US_08_AC04():
    transport = FakeTransport()
    evaluator = JevDecisionsEvaluator(transport)
    evaluator.evaluate(1, snap())
    assert set(vars(evaluator)) == {"_transport"}
    assert SECRET not in repr(evaluator)


# --- 조정기 연결 ---------------------------------------------------------------------------------


def test_coordinator_runs_adapter_unchanged_CREATE_US_08_AC02_AC04():
    """조정기는 그대로이며 어댑터를 평가기로 받아 돌린다. 결과 변환도 기존 규칙을 쓴다."""
    transport = FakeTransport(c1_age_band="under_20")
    evaluator = JevDecisionsEvaluator(transport)
    coordinator = RatingReviewCoordinator(evaluator)
    try:
        assert coordinator.publish(5, snap()) == "submitted"
        assert coordinator.wait(5)
    finally:
        coordinator.close()
    assert len(transport.requests) == 1
    outcome = review_result(evaluator.evaluate(5, snap()))
    assert (outcome.effective_rating, outcome.status) == ("SAFE", "completed")
    assert outcome.reason == "ineligible:underage_stated"
