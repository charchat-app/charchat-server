"""CREATE-US-08-AC14 영속 마지막 통과 재사용: 실제 공통 저장(HTTP)·PostgreSQL·서로 다른 조정기.

조정기(프로세스)를 새로 만들어 재시작을 흉내 낸다. 평가기는 가짜다.
"""

import re
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.features.creation.save_visibility.rating_review import (
    DatabaseReviewResults,
    RatingReviewCoordinator,
)
from app.main import create_app
from tests.features.creation.save_visibility.review_fakes import (
    ELIGIBLE,
    INELIGIBLE,
    UNAVAILABLE,
    UNDETERMINED,
    FakeEvaluator,
)
from tests.features.creation.speech_style.conftest import OWNER_ID, create_story
from tests.features.creation.speech_style.test_common_save_api import current_payload, put
from tests.features.creation.speech_style.test_working_copy_save import publish

NAMESPACE = "fake-eval.v1"
DIGEST = re.compile(r"v[0-9]+:fake-eval\.v1:[0-9a-f]{64}")


@contextmanager
def instance(db_url, verdict=ELIGIBLE, namespace=NAMESPACE, close=True):
    """프로세스 하나(앱·조정기·평가기). 나오면 앱을 닫는다(재시작).

    `close=False`면 조정기·평가기는 살려 둔다(앱만 내려간 뒤 늦게 끝나는 검토 흉내).
    """
    evaluator = FakeEvaluator(verdict)
    coordinator = RatingReviewCoordinator(
        evaluator, results=DatabaseReviewResults(db_url), reuse_namespace=namespace
    )
    try:
        with TestClient(create_app(db_url, rating_review=coordinator)) as client:
            client.headers["X-User-Id"] = str(OWNER_ID)
            yield SimpleNamespace(client=client, evaluator=evaluator, coordinator=coordinator)
    finally:
        if close:
            evaluator.gate.set()
            coordinator.close()


def save(app, story, saved, key, **extra):
    result = put(app.client, story, {**current_payload(saved), **extra}, key)
    assert result.status_code == 200, result.text
    return result.json()


def view(app, story):
    result = app.client.get(f"/api/v1/creation/stories/{story['id']}/creation")
    assert result.status_code == 200, result.text
    return result.json()


def rating(data):
    return data["effective_rating"], data["rating_review_status"]


def row(db, story):
    return db.execute(
        """SELECT effective_rating, rating_review_status, rating_requested_at,
                  rating_pass_digest, saved_at
           FROM stories WHERE id=%s""",
        (story["id"],),
    ).fetchone()


def set_world(db, story, world):
    db.execute("UPDATE stories SET world_description=%s WHERE id=%s", (world, story["id"]))


def first_pass(db, db_url, story, verdict=ELIGIBLE, namespace=NAMESPACE):
    """첫 프로세스: 공개 저장 뒤 UNSAFE 확정 저장을 한 번 검토받는다."""
    with instance(db_url, verdict, namespace) as app:
        saved = publish(app.client, story, db_url)
        unsafe = save(app, story, saved, "first-pass", requested_rating="UNSAFE")
        assert app.coordinator.wait(5)
        assert len(app.evaluator.calls) == 1
    return unsafe


def test_restart_reuses_persisted_pass_for_same_content_CREATE_US_08_AC14(db, db_url):
    story = create_story(db)
    passed = first_pass(db, db_url, story)
    stamped = row(db, story)
    assert DIGEST.fullmatch(stamped["rating_pass_digest"])
    # 지문은 해시뿐이다(원문 없음).
    assert "세계관" not in stamped["rating_pass_digest"]
    with instance(db_url) as second:
        again = save(second, story, passed, "after-restart", requested_rating="UNSAFE")
        assert second.coordinator.wait(5)
        assert second.evaluator.calls == []
        assert rating(again) == ("UNSAFE", "completed")
        assert again["rating_review_reason"] is None
        # 최신 조회는 재사용 결과, 응답 saved_at은 정본과 같다.
        latest = view(second, story)
        assert rating(latest) == ("UNSAFE", "completed")
        assert latest["saved_at"] == again["saved_at"]
        assert datetime.fromisoformat(again["saved_at"]) == row(db, story)["saved_at"]
        # 새 요청 표를 쓴다(앞선 요청 표의 늦은 결과를 막는다). 지문은 그대로다.
        reused = row(db, story)
        assert reused["rating_requested_at"] > stamped["rating_requested_at"]
        assert reused["rating_pass_digest"] == stamped["rating_pass_digest"]
        # 요청키 영수증은 커밋 시점 값 그대로다(재전송은 검토 중 응답을 돌려준다).
        replay = save(second, story, passed, "first-pass", requested_rating="UNSAFE")
        assert replay == passed
        assert rating(replay) == ("SAFE", "reviewing")
        assert second.evaluator.calls == []


def _world(db, story, saved):
    set_world(db, story, "바뀐 세계관")
    return {}


def _detail(db, story, saved):
    db.execute("UPDATE characters SET description='새 상세' WHERE story_id=%s", (story["id"],))
    return {}


def _relation(db, story, saved):
    person = db.execute("SELECT id FROM characters WHERE story_id=%s", (story["id"],)).fetchone()[
        "id"
    ]
    link = {
        "client_key": "r",
        "source_kind": "character",
        "source_character_id": person,
        "target_kind": "user",
        "target_character_id": None,
        "label": "소꿉친구",
    }
    return {"relations": [link]}


def _intro(db, story, saved):
    intros = current_payload(saved)["intros"]
    intros[0]["messages"][0]["body"] = "*눈이 내린다.*"
    return {"intros": intros}


def _fewshot(db, story, saved):
    fewshots = current_payload(saved)["fewshots"]
    fewshots[0]["messages"][0]["body"] = "새 예시"
    return {"fewshots": fewshots}


def _hashtag(db, story, saved):
    tag = db.execute("INSERT INTO tags(name) VALUES ('태그') RETURNING id").fetchone()
    db.execute("INSERT INTO story_tags(story_id,tag_id) VALUES (%s,%s)", (story["id"], tag["id"]))
    return {}


def _title(db, story, saved):
    db.execute("UPDATE stories SET title='새 제목' WHERE id=%s", (story["id"],))
    return {}


@pytest.mark.parametrize(
    ("change", "reviewed"),
    [
        (_world, True),
        (_detail, True),
        (_relation, True),
        (_intro, False),
        (_fewshot, False),
        (_hashtag, False),
        (_title, False),
    ],
)
def test_after_restart_only_reviewed_fields_trigger_new_review_CREATE_US_08_AC14(
    db, db_url, change, reviewed
):
    story = create_story(db)
    passed = first_pass(db, db_url, story)
    with instance(db_url) as second:
        extra = change(db, story, passed)
        result = save(second, story, passed, "after-restart", requested_rating="UNSAFE", **extra)
        assert second.coordinator.wait(5)
        assert len(second.evaluator.calls) == (1 if reviewed else 0)
        assert rating(view(second, story)) == ("UNSAFE", "completed")
        if not reviewed:
            assert rating(result) == ("UNSAFE", "completed")


def test_revert_reuses_old_pass_and_ignores_late_inflight_result_CREATE_US_08_AC14(db, db_url):
    story = create_story(db)
    passed = first_pass(db, db_url, story)
    digest_a = row(db, story)["rating_pass_digest"]
    # 다른 내용(B)의 검토가 첫 조정기에서 평가 중인 채로 그 앱이 내려간다.
    with instance(db_url, close=False) as old:
        set_world(db, story, "다른 세계관")
        old.evaluator.gate.clear()
        pending = save(old, story, passed, "pending-b", requested_rating="UNSAFE")
        assert rating(pending) == ("UNSAFE", "reviewing")
        assert old.evaluator.started.wait(5)
    try:
        # 다른 조정기(재시작한 프로세스)에서 예전 통과 내용(A)으로 되돌린다.
        set_world(db, story, "수정하면 안 되는 세계관")
        with instance(db_url) as new:
            reverted = save(new, story, passed, "revert-a", requested_rating="UNSAFE")
            assert new.coordinator.wait(5)
            assert new.evaluator.calls == []
            assert rating(reverted) == ("UNSAFE", "completed")
            reused_ticket = row(db, story)["rating_requested_at"]
    finally:
        # 늦게 끝난 B의 적격 결과는 쓰이지 않는다(요청 표·상태가 다르다).
        old.evaluator.gate.set()
        assert old.coordinator.wait(5)
        old.coordinator.close()
    assert len(old.evaluator.calls) == 1
    after = row(db, story)
    assert after["rating_pass_digest"] == digest_a
    assert after["rating_requested_at"] == reused_ticket
    assert (after["effective_rating"], after["rating_review_status"]) == ("UNSAFE", "completed")
    # B는 통과로 남지 않았다: B로 다시 저장하면 새로 검토한다.
    with instance(db_url) as third:
        set_world(db, story, "다른 세계관")
        save(third, story, passed, "b-again", requested_rating="UNSAFE")
        assert third.coordinator.wait(5)
        assert len(third.evaluator.calls) == 1


def test_reuse_fences_inflight_review_in_same_process_CREATE_US_08_AC14(db, db_url):
    """같은 조정기 안: 재사용이 커밋되면 대기 중인 다른 내용 검토는 평가·기록하지 않는다."""
    story = create_story(db)
    passed = first_pass(db, db_url, story)
    digest_a = row(db, story)["rating_pass_digest"]
    with instance(db_url) as app:
        app.evaluator.gate.clear()
        set_world(db, story, "다른 세계관")
        save(app, story, passed, "pending-b", requested_rating="UNSAFE")
        assert app.evaluator.started.wait(5)
        set_world(db, story, "세 번째 세계관")
        # 작업자 1개가 B에 막혀 있어 C는 대기열에 있다. 그 사이 A로 되돌려 재사용한다.
        save(app, story, passed, "pending-c", requested_rating="UNSAFE")
        set_world(db, story, "수정하면 안 되는 세계관")
        reverted = save(app, story, passed, "revert-a", requested_rating="UNSAFE")
        assert rating(reverted) == ("UNSAFE", "completed")
        app.evaluator.gate.set()
        assert app.coordinator.wait(5)
        # B는 이미 평가 중이었다. C는 최신이 아니어서 평가하지 않는다.
        assert len(app.evaluator.calls) == 1
    after = row(db, story)
    assert after["rating_pass_digest"] == digest_a
    assert (after["effective_rating"], after["rating_review_status"]) == ("UNSAFE", "completed")


@pytest.mark.parametrize("verdict", [INELIGIBLE, UNDETERMINED, UNAVAILABLE, RuntimeError("x")])
def test_rejected_or_failed_review_creates_no_fingerprint_CREATE_US_08_AC14(db, db_url, verdict):
    story = create_story(db)
    passed = first_pass(db, db_url, story, verdict=verdict)
    assert row(db, story)["rating_pass_digest"] is None
    with instance(db_url) as second:
        save(second, story, passed, "after-restart", requested_rating="UNSAFE")
        assert second.coordinator.wait(5)
        assert len(second.evaluator.calls) == 1


@pytest.mark.parametrize("verdict", [INELIGIBLE, UNAVAILABLE])
def test_noneligible_other_content_keeps_older_pass_CREATE_US_08_AC14(db, db_url, verdict):
    story = create_story(db)
    passed = first_pass(db, db_url, story)
    digest_a = row(db, story)["rating_pass_digest"]
    with instance(db_url, verdict) as app:
        set_world(db, story, "다른 세계관")
        save(app, story, passed, "other", requested_rating="UNSAFE")
        assert app.coordinator.wait(5)
        assert len(app.evaluator.calls) == 1
    assert row(db, story)["rating_pass_digest"] == digest_a
    with instance(db_url) as app:
        set_world(db, story, "수정하면 안 되는 세계관")
        reverted = save(app, story, passed, "revert", requested_rating="UNSAFE")
        assert app.coordinator.wait(5)
        assert app.evaluator.calls == []
        assert rating(reverted) == ("UNSAFE", "completed")


def test_other_namespace_does_not_reuse_and_ineligible_invalidates_same_content_CREATE_US_08_AC14(
    db, db_url
):
    story = create_story(db)
    passed = first_pass(db, db_url, story)
    # 다른 평가기(이름공간)는 재사용하지 않는다. 같은 내용을 부적격으로 보면 그 지문을 비운다.
    with instance(db_url, INELIGIBLE, namespace="other-eval.v1") as other:
        result = save(other, story, passed, "other-ns", requested_rating="UNSAFE")
        assert other.coordinator.wait(5)
        assert len(other.evaluator.calls) == 1
        assert rating(result) == ("UNSAFE", "reviewing")
    assert row(db, story)["rating_pass_digest"] is None
    assert rating(row(db, story)) == ("SAFE", "completed")
    with instance(db_url) as again:
        save(again, story, passed, "again", requested_rating="UNSAFE")
        assert again.coordinator.wait(5)
        assert len(again.evaluator.calls) == 1


def test_without_namespace_no_fingerprint_and_no_reuse_CREATE_US_08_AC14(db, db_url):
    story = create_story(db)
    passed = first_pass(db, db_url, story)
    with instance(db_url, namespace=None) as plain:
        save(plain, story, passed, "plain", requested_rating="UNSAFE")
        assert plain.coordinator.wait(5)
        # 이름공간이 없으면 재사용하지 않고, 적격 완료는 지문을 비운다(마지막 통과를 모른다).
        assert len(plain.evaluator.calls) == 1
    assert row(db, story)["rating_pass_digest"] is None


def test_working_copy_is_not_promoted_by_reuse_CREATE_US_08_AC14(db, db_url):
    """임시 저장본은 재사용 판단에 쓰지 않고, 임시 저장은 적용 등급을 바꾸지 않는다."""
    story = create_story(db)
    passed = first_pass(db, db_url, story)
    with instance(db_url) as app:
        # 정본을 SAFE로 확정 저장해 적용 등급을 SAFE로 둔다(지문은 남는다).
        safe = save(app, story, passed, "safe", requested_rating="SAFE")
        assert rating(safe) == ("SAFE", "not_reviewed")
        staged = save(app, story, safe, "stage", kind="temporary", requested_rating="UNSAFE")
        assert staged["requested_rating"] == "UNSAFE"
        assert rating(staged) == ("SAFE", "not_reviewed")
        assert rating(view(app, story)) == ("SAFE", "not_reviewed")
        assert app.coordinator.wait(5)
        assert app.evaluator.calls == []
        # 「저장」으로 확정해야 같은 정본 내용의 통과를 재사용한다.
        final = save(app, story, safe, "commit")
        assert rating(final) == ("UNSAFE", "completed")
        assert app.coordinator.wait(5)
        assert app.evaluator.calls == []


def test_fingerprint_stamp_keeps_saved_at_CREATE_US_08_AC03_AC14(db, db_url):
    story = create_story(db)
    with instance(db_url) as app:
        saved = publish(app.client, story, db_url)
        app.evaluator.gate.clear()
        pending = save(app, story, saved, "first-pass", requested_rating="UNSAFE")
        assert app.evaluator.started.wait(5)
        before = row(db, story)["saved_at"]
        app.evaluator.gate.set()
        assert app.coordinator.wait(5)
        after = row(db, story)
        assert after["rating_pass_digest"] is not None
        assert after["saved_at"] == before
        assert view(app, story)["saved_at"] == pending["saved_at"]


def test_legacy_writer_same_content_rejection_is_not_reused_CREATE_US_08_AC14(db, db_url):
    """롤아웃 중 지문을 모르는 이전 버전이 같은 내용 A를 거부하면 A를 재사용하지 않는다."""
    story = create_story(db)
    passed = first_pass(db, db_url, story)
    assert DIGEST.fullmatch(row(db, story)["rating_pass_digest"])
    # 이전 버전 프로세스: 같은 내용 A로 확정 저장(검토 중·새 요청 표) 뒤 거부 결과를 쓴다.
    # 기준 커밋(fcc8a9e)의 SQL과 같은 SET 목록이라 지문 컬럼을 건드리지 않는다.
    db.execute(
        """UPDATE stories SET rating_review_status='reviewing',
               rating_requested_at=clock_timestamp() WHERE id=%s""",
        (story["id"],),
    )
    db.execute(
        """UPDATE stories SET effective_rating=COALESCE('SAFE', effective_rating),
               rating_review_status='completed', rating_review_reason='ineligible:underage_stated'
           WHERE id=%s AND requested_rating='UNSAFE' AND rating_review_status='reviewing'""",
        (story["id"],),
    )
    assert row(db, story)["rating_pass_digest"] is None
    with instance(db_url) as app:
        result = save(app, story, passed, "after-legacy", requested_rating="UNSAFE")
        assert rating(result) == ("SAFE", "reviewing")
        assert app.coordinator.wait(5)
        assert len(app.evaluator.calls) == 1
