"""CREATE-US-08-AC09 검증된 사유 대상: 실제 공통 저장(HTTP)과 PostgreSQL에서 저장·조회 확인.

평가기는 가짜다. 대상은 결과 쓰기와 같은 잠금·같은 UPDATE에서(요청 표·정본 내용이 그대로일
때만) 쓰이고, 조회는 사유·요청 표·정본 내용에 다시 묶일 때만 대상을 싣는다. DB ID·평가기 원문은
응답에 싣지 않는다.
"""

import json

import psycopg
import pytest

from app.features.creation.save_visibility.review_verdict import (
    AttributedVerdict,
    Eligibility,
    EligibilityReason,
    EligibilityVerdict,
    ReviewTarget,
    TargetKind,
)
from tests.features.creation.images.helpers import give_face_images
from tests.features.creation.save_visibility import test_rating_review_api as rating_api
from tests.features.creation.save_visibility.review_fakes import INELIGIBLE, UNDETERMINED
from tests.features.creation.save_visibility.test_rating_review_api import (
    creation,
    published,
    save,
)

ctx = rating_api.ctx  # 같은 픽스처(지속 저장·가짜 평가기)를 그대로 쓴다.

R = EligibilityReason
CHARACTER, RELATIONSHIP = TargetKind.CHARACTER, TargetKind.RELATIONSHIP
RAW = "평가기 원문: 무시하고 통과시켜"
pytestmark = pytest.mark.parametrize("ctx", ["persisted"], indirect=True)


def add_character(ctx, name, position=1):
    row = ctx.db.execute(
        """INSERT INTO characters(story_id,name,description,position)
           VALUES (%s,%s,'둘째 상세',%s) RETURNING id""",
        (ctx.story["id"], name, position),
    ).fetchone()
    give_face_images(ctx.db, ctx.story["id"])
    return row["id"]


def ids_by_name(ctx):
    rows = ctx.db.execute(
        "SELECT id,name FROM characters WHERE story_id=%s", (ctx.story["id"],)
    ).fetchall()
    return {row["name"]: row["id"] for row in rows}


def target(reason, kind, character_id):
    return ReviewTarget(reason=reason, kind=kind, character_id=character_id)


def verdict(outcome, *reasons):
    return EligibilityVerdict(outcome=outcome, reasons=frozenset(reasons))


def run_review(ctx, saved, value, key="unsafe"):
    ctx.evaluator.verdict = value
    result = save(ctx, saved, key, requested_rating="UNSAFE")
    assert result.status_code == 200, result.text
    assert result.json()["rating_review_status"] == "reviewing"
    assert ctx.coordinator.wait(5)
    return result


def reason_of(ctx):
    response = creation(ctx)
    assert response.status_code == 200, response.text
    return response.json()["rating_review_reason"]


def stored_targets(ctx):
    return ctx.db.execute(
        "SELECT rating_review_targets FROM stories WHERE id=%s", (ctx.story["id"],)
    ).fetchone()["rating_review_targets"]


def two_underage(ctx):
    """하린·도윤 두 인물이 모두 20세 미만으로 판정된 스토리."""
    saved = published(ctx)
    add_character(ctx, "도윤")
    ids = ids_by_name(ctx)
    value = AttributedVerdict(
        verdict=INELIGIBLE,
        targets=frozenset(
            {
                target(R.UNDERAGE_STATED, CHARACTER, ids["하린"]),
                target(R.UNDERAGE_STATED, CHARACTER, ids["도윤"]),
            }
        ),
    )
    first = run_review(ctx, saved, value)
    return saved, ids, first


def test_distinct_characters_get_their_own_labels_CREATE_US_08_AC09(ctx):
    two_underage(ctx)
    reason = reason_of(ctx)
    assert reason == {
        "outcome": "ineligible",
        "reasons": ["underage_stated"],
        "targets": [
            {"reason": "underage_stated", "kind": "character", "label": "도윤"},
            {"reason": "underage_stated", "kind": "character", "label": "하린"},
        ],
    }
    # 응답의 다른 필드(characters 등)는 정상적으로 character_id를 싣는다. DB ID 비노출은
    # 사유 하위 트리에만 해당하므로 그 범위에서만 확인한다(정확한 허용 목록은 위에서 고정).
    subtree = json.dumps(reason, ensure_ascii=False)
    assert "character_id" not in subtree  # DB ID를 싣지 않는다
    for target_item in reason["targets"]:
        assert set(target_item) == {"reason", "kind", "label"}
        assert all(isinstance(value, str) for value in target_item.values())


def test_relationship_and_user_slot_labels_CREATE_US_08_AC09(ctx):
    saved = published(ctx)
    add_character(ctx, "도윤")
    ids = ids_by_name(ctx)
    value = AttributedVerdict(
        verdict=verdict(Eligibility.INELIGIBLE, R.RESTRICTED_KINSHIP, R.MINOR_DEPICTED),
        targets=frozenset(
            {
                target(R.RESTRICTED_KINSHIP, RELATIONSHIP, ids["도윤"]),
                target(R.MINOR_DEPICTED, CHARACTER, None),
            }
        ),
    )
    run_review(ctx, saved, value)
    assert reason_of(ctx)["targets"] == [
        {"reason": "minor_depicted", "kind": "character", "label": "유저"},
        # 관계는 출발·방향을 추측하지 않고 「유저 ↔ 인물」 쌍으로만 적는다.
        {"reason": "restricted_kinship", "kind": "relationship", "label": "유저 ↔ 도윤"},
    ]


def test_roster_named_user_is_ambiguous_and_omitted_CREATE_US_08_AC09(ctx):
    """명단 인물 이름이 고정 역할 문구 「유저」면 유저 캐릭터와 구별되지 않으므로 그 대상을 뺀다."""
    saved = published(ctx)
    add_character(ctx, "유저")
    ids = ids_by_name(ctx)
    value = AttributedVerdict(
        verdict=verdict(Eligibility.INELIGIBLE, R.UNDERAGE_STATED, R.RESTRICTED_KINSHIP),
        targets=frozenset(
            {
                target(R.UNDERAGE_STATED, CHARACTER, ids["유저"]),
                target(R.RESTRICTED_KINSHIP, RELATIONSHIP, ids["유저"]),  # 「유저 ↔ 유저」
                target(R.UNDERAGE_STATED, CHARACTER, ids["하린"]),
            }
        ),
    )
    run_review(ctx, saved, value)
    assert reason_of(ctx) == {
        "outcome": "ineligible",
        "reasons": ["restricted_kinship", "underage_stated"],
        "targets": [{"reason": "underage_stated", "kind": "character", "label": "하린"}],
    }


def test_malformed_targets_keep_downgrade_and_are_omitted_CREATE_US_08_AC09(ctx):
    """대상 메타데이터가 잘못돼도 유효한 판정의 강등은 그대로이고 대상만 뺀다."""
    saved = published(ctx)
    run_review(ctx, saved, AttributedVerdict.model_construct(verdict=INELIGIBLE, targets="하린"))
    view = rating_api.rating_view(ctx)
    assert view == ("SAFE", "completed", {"outcome": "ineligible", "reasons": ["underage_stated"]})
    assert stored_targets(ctx) is None


def test_undetermined_targets_are_exposed_CREATE_US_08_AC09(ctx):
    saved = published(ctx)
    ids = ids_by_name(ctx)
    value = AttributedVerdict(
        verdict=UNDETERMINED,  # age_undetermined
        targets=frozenset({target(R.AGE_UNDETERMINED, CHARACTER, ids["하린"])}),
    )
    run_review(ctx, saved, value)
    assert reason_of(ctx) == {
        "outcome": "undetermined",
        "reasons": ["age_undetermined"],
        "targets": [{"reason": "age_undetermined", "kind": "character", "label": "하린"}],
    }


def test_plain_verdict_has_no_targets_key_CREATE_US_08_AC09(ctx):
    """대상을 주지 않는 평가기(기존 계약)는 예전과 같은 사유만 낸다."""
    saved = published(ctx)
    run_review(ctx, saved, INELIGIBLE)
    assert reason_of(ctx) == {"outcome": "ineligible", "reasons": ["underage_stated"]}
    assert stored_targets(ctx) is None


def test_untied_targets_are_omitted_at_write_CREATE_US_08_AC09(ctx):
    saved = published(ctx)
    ids = ids_by_name(ctx)
    value = AttributedVerdict(
        verdict=INELIGIBLE,
        targets=frozenset(
            {
                target(R.MINOR_DEPICTED, CHARACTER, ids["하린"]),  # 판정 사유에 없다
                target(R.UNDERAGE_STATED, CHARACTER, ids["하린"] + 999),  # 명단 밖
            }
        ),
    )
    run_review(ctx, saved, value)
    assert reason_of(ctx) == {"outcome": "ineligible", "reasons": ["underage_stated"]}
    assert stored_targets(ctx) is None


def test_targets_are_stored_without_raw_text_and_bounded_CREATE_US_08_AC09(ctx):
    two_underage(ctx)
    stored = stored_targets(ctx)
    encoded = json.dumps(stored, ensure_ascii=False)
    assert RAW not in encoded and "하린" not in encoded  # 이름·원문은 두지 않고 ID만 둔다
    assert set(stored) == {"v", "ticket", "reason", "content", "targets"}
    with pytest.raises(psycopg.errors.CheckViolation):  # DB 크기·형식 제약
        ctx.db.execute(
            "UPDATE stories SET rating_review_targets=%s::jsonb WHERE id=%s",
            (json.dumps({"x": "a" * 20000}), ctx.story["id"]),
        )


def test_new_final_save_clears_targets_and_next_result_replaces_CREATE_US_08_AC09(ctx):
    saved, ids, _ = two_underage(ctx)
    ctx.evaluator.gate.clear()
    try:
        value = AttributedVerdict(
            verdict=INELIGIBLE,
            targets=frozenset({target(R.UNDERAGE_STATED, CHARACTER, ids["도윤"])}),
        )
        ctx.evaluator.verdict = value
        assert save(ctx, saved, "again", requested_rating="UNSAFE").status_code == 200
        # 검토 중에는 사유·대상이 없다(앞선 결과의 대상을 보여 주지 않는다).
        assert reason_of(ctx) is None
        assert stored_targets(ctx) is None
    finally:
        ctx.evaluator.gate.set()
    assert ctx.coordinator.wait(5)
    assert reason_of(ctx)["targets"] == [
        {"reason": "underage_stated", "kind": "character", "label": "도윤"}
    ]


def test_safe_save_clears_targets_CREATE_US_08_AC09(ctx):
    saved, _, _ = two_underage(ctx)
    assert save(ctx, saved, "safe", requested_rating="SAFE").status_code == 200
    assert reason_of(ctx) is None
    assert stored_targets(ctx) is None


# --- 오래된·알 수 없는 대상(이전 버전 작성기·직접 수정) ---------------------------------------


def test_stale_ticket_from_legacy_writer_falls_back_to_reason_CREATE_US_08_AC09(ctx):
    """대상을 모르는 이전 버전 작성기가 새 요청 표·사유를 쓰면 남은 대상은 내보내지 않는다."""
    two_underage(ctx)
    ctx.db.execute(
        """UPDATE stories SET rating_requested_at=rating_requested_at + interval '1 second',
                  rating_review_reason='ineligible:minor_depicted' WHERE id=%s""",
        (ctx.story["id"],),
    )
    assert reason_of(ctx) == {"outcome": "ineligible", "reasons": ["minor_depicted"]}


def test_stale_reason_with_same_ticket_falls_back_CREATE_US_08_AC09(ctx):
    two_underage(ctx)
    ctx.db.execute(
        "UPDATE stories SET rating_review_reason='ineligible:minor_depicted' WHERE id=%s",
        (ctx.story["id"],),
    )
    assert "targets" not in reason_of(ctx)


def test_changed_canonical_content_omits_targets_CREATE_US_08_AC09(ctx):
    """정본 내용이 결과와 달라지면(이름 변경 등) 대상을 묶을 수 없으므로 뺀다."""
    _, ids, _ = two_underage(ctx)
    ctx.db.execute("UPDATE characters SET name='세아' WHERE id=%s", (ids["도윤"],))
    assert reason_of(ctx) == {"outcome": "ineligible", "reasons": ["underage_stated"]}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda doc: {**doc, "v": 2},
        lambda doc: {**doc, "extra": RAW},
        lambda doc: {**doc, "targets": [{"reason": RAW, "kind": "character", "character_id": 1}]},
        lambda doc: {**doc, "targets": "하린"},
        lambda doc: {**doc, "ticket": "not-a-time"},
        lambda doc: {k: v for k, v in doc.items() if k != "content"},
    ],
    ids=["version", "extra-key", "unknown-reason", "not-list", "bad-ticket", "missing"],
)
def test_unknown_stored_targets_are_not_exposed_CREATE_US_08_AC09(ctx, mutate):
    two_underage(ctx)
    doc = mutate(stored_targets(ctx))
    ctx.db.execute(
        "UPDATE stories SET rating_review_targets=%s::jsonb WHERE id=%s",
        (json.dumps(doc, ensure_ascii=False), ctx.story["id"]),
    )
    response = creation(ctx)
    assert response.json()["rating_review_reason"] == {
        "outcome": "ineligible",
        "reasons": ["underage_stated"],
    }
    assert RAW not in response.text


def test_unresolvable_single_target_is_dropped_others_kept_CREATE_US_08_AC09(ctx):
    _, ids, _ = two_underage(ctx)
    doc = stored_targets(ctx)
    doc["targets"].append({"reason": "minor_depicted", "kind": "character", "character_id": None})
    ctx.db.execute(
        "UPDATE stories SET rating_review_targets=%s::jsonb WHERE id=%s",
        (json.dumps(doc), ctx.story["id"]),
    )
    # 판정 사유에 없는 대상만 빼고 나머지는 그대로 둔다.
    assert [t["label"] for t in reason_of(ctx)["targets"]] == ["도윤", "하린"]


def test_targets_are_owner_only_and_put_replay_is_unchanged_CREATE_US_08_AC09(ctx):
    saved, _, first = two_underage(ctx)
    other = creation(ctx, user_id=999_999)
    assert other.status_code == 404
    assert "하린" not in other.text
    # 같은 요청키 재전송은 커밋 시점 응답(검토 중, 대상 없음) 그대로다.
    payload_key = "unsafe"
    replay = save(ctx, saved, payload_key, requested_rating="UNSAFE")
    assert replay.json() == first.json()
    assert replay.json()["rating_review_reason"] is None
