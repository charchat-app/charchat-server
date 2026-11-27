"""CREATE-US-08: 기본 앱의 등급 검토 조정기 활성화(`RATING_REVIEW_ENABLED`).

아래쪽 오프라인 통합 확인은 실제 `create_app`을 설정으로 켜서 만들고, 전송 지점
(`rating_runtime.jev_transport`)만 가짜로 바꾼다. 공급자를 부르지 않으며 DB는 폐기 가능한 테스트
DB 픽스처(`db`·`db_url`)만 쓴다. 공통 안전 판정기(AC15)는 설정으로 만들지 않고 주입할 때만 쓰므로
여기서 다루지 않는다.
"""

import re
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app import main
from app.core.config import Settings
from app.features.creation.save_visibility import runtime as rating_runtime
from app.features.creation.save_visibility.rating_review import (
    RatingReviewCoordinator,
    ReviewSnapshot,
    encode_reason,
    reason_payload,
    unavailable,
)
from tests.features.creation.save_visibility.test_jev_decisions import FakeTransport
from tests.features.creation.save_visibility.test_rating_review_api import rating_view
from tests.features.creation.speech_style.conftest import OWNER_ID, create_story
from tests.features.creation.speech_style.test_common_save_api import current_payload, put
from tests.features.creation.speech_style.test_common_save_rating import (
    rating_row,
    staged_rating,
)
from tests.features.creation.speech_style.test_working_copy_save import publish


@pytest.fixture
def configured(monkeypatch):
    class TestSettings(Settings):
        rating_review_enabled: bool = False
        jev_openrouter_api_key: SecretStr | None = SecretStr("synthetic-test")

    settings = TestSettings(_env_file=None, database_url=None, app_env="test")
    monkeypatch.setattr(main, "settings", settings)
    events = []
    monkeypatch.setattr(main, "open_pool", lambda url: events.append("open"))
    monkeypatch.setattr(main, "close_pool", lambda: events.append("pool-close"))
    # 앱이 소유한 조정기는 `drain`(정상 종료)으로, 주입·기존 호출자는 `close`로 닫는다.
    # 어느 쪽이든 조정기 종료를 같은 사건으로 기록한다.
    original_close = RatingReviewCoordinator.close
    original_drain = RatingReviewCoordinator.drain

    def close(coordinator):
        events.append("review-close")
        original_close(coordinator)

    def drain(coordinator):
        events.append("review-close")
        original_drain(coordinator)

    monkeypatch.setattr(RatingReviewCoordinator, "close", close)
    monkeypatch.setattr(RatingReviewCoordinator, "drain", drain)
    return settings, events


@pytest.mark.parametrize("rating", [False, True])
def test_flag_and_owned_lifecycle_CREATE_US_08_AC02(configured, rating):
    settings, events = configured
    settings.rating_review_enabled = rating
    app = main.create_app("postgresql://synthetic")
    with TestClient(app):
        assert isinstance(app.state.rating_review, RatingReviewCoordinator) == rating
        assert app.state.common_safety is None  # 공통 안전 판정기는 설정으로 만들지 않는다.
        if rating:
            assert app.state.rating_review.persists
    assert events == ["open", *(["review-close"] if rating else []), "pool-close"]


def test_enabled_rating_requires_credential_CREATE_US_08_AC02(configured):
    settings, events = configured
    settings.rating_review_enabled = True
    settings.jev_openrouter_api_key = SecretStr(" ")
    with pytest.raises((ValueError, RuntimeError), match="JEV_OPENROUTER_API_KEY"):
        with TestClient(main.create_app("postgresql://synthetic")):
            pass
    assert "open" not in events


def test_enabled_rating_requires_database_CREATE_US_08_AC02(configured):
    settings, events = configured
    settings.rating_review_enabled = True
    with pytest.raises((ValueError, RuntimeError), match="DATABASE_URL"):
        with TestClient(main.create_app()):
            pass
    assert "open" not in events


def test_injected_coordinator_stays_caller_owned_CREATE_US_08_AC02(configured):
    settings, events = configured
    settings.rating_review_enabled = True
    settings.jev_openrouter_api_key = None
    coordinator = RatingReviewCoordinator(lambda _: None)
    try:
        with TestClient(
            main.create_app("postgresql://synthetic", rating_review=coordinator)
        ) as client:
            assert client.app.state.rating_review is coordinator
        assert events == ["open", "pool-close"]
    finally:
        coordinator.close()


class GatedEvaluator:
    """첫 평가가 시작되면 알리고, 관문이 열릴 때까지 모든 평가를 붙잡는다."""

    def __init__(self, events):
        self.events = events
        self.started = threading.Event()
        self.gate = threading.Event()

    def evaluate(self, story_id, snapshot):
        self.events.append(f"start:{story_id}")
        self.started.set()
        assert self.gate.wait(5), "관문이 열리지 않았다"
        self.events.append(f"done:{story_id}")
        return None


def _snapshot(text):
    return ReviewSnapshot(text, (), ())


def test_graceful_shutdown_drains_queued_and_running_reviews_before_pool_close_CREATE_US_08_AC02(
    configured, monkeypatch
):
    """종료는 평가 중인 검토와 대기열의 검토를 모두 끝낸 뒤에 DB 풀을 닫는다.

    작업자 하나에 검토 둘을 넘겨 하나는 평가 중, 하나는 대기열에 둔다. 종료를 시작한 뒤 관문을
    열어 두 검토가 모두 평가되고(대기열 검토를 취소하지 않는다) 그 뒤에 풀이 닫히는지 본다.
    """
    settings, events = configured
    settings.rating_review_enabled = True
    evaluator = GatedEvaluator(events)
    owned = []

    def coordinator(transport, url):
        owned.append(RatingReviewCoordinator(evaluator, max_workers=1))
        return owned[-1]

    monkeypatch.setattr(rating_runtime, "jev_transport", lambda api_key: object())
    monkeypatch.setattr(rating_runtime, "rating_review_coordinator", coordinator)

    app = main.create_app("postgresql://synthetic")
    with TestClient(app):
        review = app.state.rating_review
        assert review is owned[0]
        assert review.publish(1, _snapshot("평가 중")) == "submitted"
        assert review.publish(2, _snapshot("대기열")) == "submitted"
        assert evaluator.started.wait(5)
        assert "start:2" not in events  # 두 번째는 대기열에 있다.
        # 종료가 시작된 뒤에 관문을 연다.
        opener = threading.Timer(0.2, evaluator.gate.set)
        opener.start()
    opener.join()

    assert app.state.rating_review is None
    assert "review-close" in events
    assert events[-1] == "pool-close"
    assert "done:1" in events and "done:2" in events  # 대기열 검토도 취소하지 않고 끝낸다.
    pool_closed = events.index("pool-close")
    assert events.index("done:1") < pool_closed
    assert events.index("done:2") < pool_closed
    assert owned[0].wait(0)


# --- 오프라인 통합: 실제 앱·테스트 DB·가짜 전송 ---


@pytest.fixture
def runtime(db, db_url, monkeypatch):
    """`runtime(rating)`: 설정을 바꾼 뒤 실제 `create_app(db_url)`을 연다.

    키는 비워 두고 전송 생성 지점만 가짜로 바꾼다(실제 키·네트워크 없음).
    """
    transport = FakeTransport()
    keys = []

    def fake_transport(api_key):
        keys.append(api_key)
        return transport

    monkeypatch.setattr(main.settings, "jev_openrouter_api_key", None)
    monkeypatch.setattr(rating_runtime, "jev_transport", fake_transport)

    @contextmanager
    def run(rating=True):
        monkeypatch.setattr(main.settings, "rating_review_enabled", rating)
        app = main.create_app(db_url)
        with TestClient(app) as client:
            client.headers["X-User-Id"] = str(OWNER_ID)
            if rating:
                assert keys[-1] is None  # 설정 값을 그대로 넘겼고 가짜 전송을 받았다.
            assert isinstance(app.state.rating_review, RatingReviewCoordinator) == rating
            assert app.state.common_safety is None
            yield SimpleNamespace(client=client, app=app, transport=transport)
        assert app.state.rating_review is None  # 앱이 만든 조정기는 종료 때 내려놓는다.

    return run


def _unsafe_save(ctx, saved, key):
    payload = {**current_payload(saved), "requested_rating": "UNSAFE"}
    result = put(ctx.client, ctx.story, payload, key)
    assert result.status_code == 200, result.text
    data = result.json()
    assert ctx.app.state.rating_review.wait(5)
    return data["effective_rating"], data["rating_review_status"]


def _pass_digest(db, story):
    return db.execute(
        "SELECT rating_pass_digest FROM stories WHERE id=%s", (story["id"],)
    ).fetchone()["rating_pass_digest"]


def test_enabled_runtime_reviews_unsafe_saves_into_db_CREATE_US_08_AC02_AC11_AC14(
    db, db_url, runtime
):
    """첫 UNSAFE 확정 저장은 적격으로 UNSAFE가 되고, 검토 대상 수정 뒤 공급자 장애는 앞선
    UNSAFE를 유지한 실패로 남으며, 다음 저장에서 다시 검토해 완료된다(모두 DB 상태로 확인)."""
    with runtime() as app:
        story = create_story(db)
        ctx = SimpleNamespace(client=app.client, app=app.app, story=story)
        decisions = app.transport
        eligible = decisions.respond

        saved = publish(app.client, story, db_url)
        assert app.app.state.rating_review.wait(5)
        assert decisions.requests == []  # SAFE 저장은 보내지 않는다.

        # AC02: 첫 UNSAFE 저장은 커밋 시점 SAFE·검토 중, 적격 결과로 UNSAFE·완료.
        assert _unsafe_save(ctx, saved, "first") == ("SAFE", "reviewing")
        assert len(decisions.requests) == 1
        assert rating_view(ctx) == ("UNSAFE", "completed", None)
        first = rating_row(db_url, story)
        assert first["requested_rating"] == "UNSAFE"
        assert first["rating_requested_at"] is not None
        # AC14: 앱이 만든 조정기는 고정 이름공간으로 통과 지문(해시만)을 남긴다.
        namespace = re.escape(rating_runtime.REUSE_NAMESPACE)
        digest = _pass_digest(db, story)
        assert re.fullmatch(rf"v[0-9]+:{namespace}:[0-9a-f]{{64}}", digest)

        # AC11: 검토 대상(세계관) 수정 뒤 전송 장애 → 앞선 UNSAFE 유지, 실패·허용 코드만.
        db.execute("UPDATE stories SET world_description='새 세계관' WHERE id=%s", (story["id"],))
        decisions.respond = lambda request: OSError("connection reset")
        assert _unsafe_save(ctx, saved, "outage") == ("UNSAFE", "reviewing")
        assert len(decisions.requests) == 2  # 재시도하지 않는다.
        failure = {"outcome": "unavailable", "reasons": ["evaluator_failure"]}
        assert rating_view(ctx) == ("UNSAFE", "failed", failure)
        failed = rating_row(db_url, story)
        assert failed["rating_requested_at"] > first["rating_requested_at"]
        assert "connection reset" not in (failed["rating_review_reason"] or "")
        assert _pass_digest(db, story) == digest  # 실패는 통과 지문을 바꾸지 않는다.

        # 장애 회복 뒤 같은 내용을 다시 저장하면 새로 검토해 완료된다.
        decisions.respond = eligible
        assert _unsafe_save(ctx, saved, "recovered") == ("UNSAFE", "reviewing")
        assert len(decisions.requests) == 3
        assert rating_view(ctx) == ("UNSAFE", "completed", None)
        assert rating_row(db_url, story)["rating_requested_at"] > failed["rating_requested_at"]
        assert _pass_digest(db, story) != digest


def test_disabled_explicit_safe_save_clears_prior_approved_unsafe_CREATE_US_08_AC02_AC13(
    db, db_url, runtime
):
    """켜진 앱에서 UNSAFE가 승인된 뒤, 설정을 끈 앱에서도:

    - SAFE 임시 저장은 임시 저장본에만 남고 저장본의 적용 등급·검토 상태를 바꾸지 않는다(AC13).
    - SAFE를 명시한 확정 저장이 커밋되면 적용 등급을 SAFE로, 검토 상태를 not_reviewed로 되돌린다
      (앞선 UNSAFE 승인이 남아 SAFE 선택 스토리가 UNSAFE로 노출되지 않는다, AC02).
    """
    story = create_story(db)
    with runtime() as on:
        ctx = SimpleNamespace(client=on.client, app=on.app, story=story)
        saved = publish(on.client, story, db_url)
        assert on.app.state.rating_review.wait(5)
        assert _unsafe_save(ctx, saved, "approved") == ("SAFE", "reviewing")
        assert rating_view(ctx) == ("UNSAFE", "completed", None)
    approved = rating_row(db_url, story)
    assert approved["requested_rating"] == "UNSAFE"
    assert approved["effective_rating"] == "UNSAFE"

    with runtime(rating=False) as off:
        ctx = SimpleNamespace(client=off.client, story=story)

        # AC13: 임시 SAFE는 임시 저장본에만 둔다.
        staged = {**current_payload(saved), "kind": "temporary", "requested_rating": "SAFE"}
        result = put(off.client, story, staged, "off-stage")
        assert result.status_code == 200, result.text
        data = result.json()
        assert data["requested_rating"] == "SAFE"
        assert data["effective_rating"] == "UNSAFE"
        assert data["rating_review_status"] == "completed"
        assert rating_row(db_url, story) == approved
        assert staged_rating(db_url, story) == "SAFE"
        assert rating_view(ctx) == ("UNSAFE", "completed", None)

        # AC02: 명시적 SAFE 확정 저장은 커밋과 함께 적용 등급·검토 상태를 비운다.
        committed = {**current_payload(saved), "requested_rating": "SAFE"}
        result = put(off.client, story, committed, "off-commit")
        assert result.status_code == 200, result.text
        data = result.json()
        assert data["requested_rating"] == "SAFE"
        assert data["effective_rating"] == "SAFE"
        assert data["rating_review_status"] == "not_reviewed"
        assert data["working_copy_saved_at"] is None
        assert rating_view(ctx) == ("SAFE", "not_reviewed", None)

    after = rating_row(db_url, story)
    assert after["requested_rating"] == "SAFE"
    assert after["effective_rating"] == "SAFE"
    assert after["rating_review_status"] == "not_reviewed"
    assert after["rating_review_reason"] is None
    # 요청 표는 마지막 검토 요청 시각이라 지우지 않는다(`mark_rating_safe`와 같은 규칙).
    assert after["rating_requested_at"] == approved["rating_requested_at"]
    assert staged_rating(db_url, story) is None


# --- 설정 꺼짐: SAFE 확정 저장 분기(조정기 없음) ---


def _seed_rating(db, story, effective, status, reason=None):
    """앞선 UNSAFE 선택 뒤의 저장본 등급 상태를 테스트 DB에 직접 만든다(검토 메타데이터만)."""
    db.execute(
        """UPDATE stories SET requested_rating='UNSAFE', effective_rating=%s,
               rating_review_status=%s, rating_review_reason=%s,
               rating_requested_at=clock_timestamp()
           WHERE id=%s""",
        (effective, status, reason, story["id"]),
    )


def _assert_saved(result, effective, status):
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["requested_rating"] == "SAFE"
    assert data["effective_rating"] == effective
    assert data["rating_review_status"] == status
    assert data["working_copy_saved_at"] is None


def test_disabled_staged_safe_promoted_without_field_clears_unsafe_CREATE_US_08_AC02_AC13(
    db, db_url, runtime
):
    """설정 꺼짐: 임시 저장한 SAFE 선택을 `requested_rating` 없이 확정 저장으로 반영하면, 반영한
    SAFE 선택으로 앞선 UNSAFE 적용 등급·검토 상태를 SAFE·not_reviewed로 되돌린다."""
    story = create_story(db)
    with runtime(rating=False) as off:
        ctx = SimpleNamespace(client=off.client, story=story)
        saved = publish(off.client, story, db_url)
        _seed_rating(db, story, "UNSAFE", "completed")
        approved = rating_row(db_url, story)

        staged = {**current_payload(saved), "kind": "temporary", "requested_rating": "SAFE"}
        result = put(off.client, story, staged, "staged-safe")
        assert result.status_code == 200, result.text
        assert rating_row(db_url, story) == approved  # AC13: 임시 저장은 저장본을 바꾸지 않는다.
        assert staged_rating(db_url, story) == "SAFE"

        committed = current_payload(saved)
        assert "requested_rating" not in committed  # 필드를 보내지 않는다.
        _assert_saved(put(off.client, story, committed, "promote"), "SAFE", "not_reviewed")
        assert rating_view(ctx) == ("SAFE", "not_reviewed", None)

    after = rating_row(db_url, story)
    assert after["requested_rating"] == "SAFE"
    assert after["effective_rating"] == "SAFE"
    assert after["rating_review_status"] == "not_reviewed"
    assert after["rating_review_reason"] is None
    assert after["rating_requested_at"] == approved["rating_requested_at"]
    assert staged_rating(db_url, story) is None


def test_disabled_safe_save_invalidates_pending_safe_review_CREATE_US_08_AC02(db, db_url, runtime):
    """설정 꺼짐: 적용 등급 SAFE로 검토 중(첫 UNSAFE 검토 대기)인 저장본에 SAFE를 명시해 확정
    저장하면 검토 상태를 not_reviewed로 바꿔 대기 중인 검토를 무효로 한다."""
    story = create_story(db)
    with runtime(rating=False) as off:
        ctx = SimpleNamespace(client=off.client, story=story)
        saved = publish(off.client, story, db_url)
        _seed_rating(db, story, "SAFE", "reviewing")
        pending = rating_row(db_url, story)

        committed = {**current_payload(saved), "requested_rating": "SAFE"}
        _assert_saved(put(off.client, story, committed, "pending-safe"), "SAFE", "not_reviewed")
        assert rating_view(ctx) == ("SAFE", "not_reviewed", None)

    after = rating_row(db_url, story)
    assert after == {
        **pending,
        "requested_rating": "SAFE",
        "rating_review_status": "not_reviewed",
        "rating_review_reason": None,
    }


def test_disabled_safe_save_preserves_safe_failed_metadata_CREATE_US_08_AC02_AC13(
    db, db_url, runtime
):
    """설정 꺼짐: 이미 적용 등급 SAFE인 실패(failed) 메타데이터는 SAFE 확정 저장에서 그대로 둔다
    (선택만 SAFE로 바뀌고 적용 등급·검토 상태·사유·요청 표는 유지)."""
    reason = encode_reason(unavailable())
    story = create_story(db)
    with runtime(rating=False) as off:
        ctx = SimpleNamespace(client=off.client, story=story)
        saved = publish(off.client, story, db_url)
        _seed_rating(db, story, "SAFE", "failed", reason)
        failed = rating_row(db_url, story)

        committed = {**current_payload(saved), "requested_rating": "SAFE"}
        _assert_saved(put(off.client, story, committed, "failed-safe"), "SAFE", "failed")
        assert rating_view(ctx) == ("SAFE", "failed", reason_payload(reason))

    assert rating_row(db_url, story) == {**failed, "requested_rating": "SAFE"}
