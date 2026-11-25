"""CREATE-US-08-AC02·AC04·AC09·AC11 Jev 어댑터를 주입한 공통 저장(HTTP)·PostgreSQL 종단 확인.

공급자는 가짜 전송이다(네트워크 없음). 조정기·결과 보관소는 테스트에서만 주입하며 운영 앱은
여전히 조정기 없이 만들어진다.
"""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.features.creation.save_visibility.jev_decisions import JevDecisionsEvaluator
from app.features.creation.save_visibility.rating_review import (
    DatabaseReviewResults,
    RatingReviewCoordinator,
)
from app.main import create_app
from tests.features.creation.save_visibility.test_jev_decisions import INVALID, FakeTransport
from tests.features.creation.save_visibility.test_rating_review_api import rating_view
from tests.features.creation.speech_style.conftest import OWNER_ID, create_story
from tests.features.creation.speech_style.test_common_save_api import current_payload, put
from tests.features.creation.speech_style.test_working_copy_save import publish


@pytest.fixture
def jev(db, db_url):
    def run(transport):
        coordinator = RatingReviewCoordinator(
            JevDecisionsEvaluator(transport), results=DatabaseReviewResults(db_url)
        )
        client = TestClient(create_app(db_url, rating_review=coordinator))
        client.headers["X-User-Id"] = str(OWNER_ID)
        contexts.append((client, coordinator))
        client.__enter__()
        story = create_story(db)
        saved = publish(client, story, db_url)
        assert coordinator.wait(5)
        assert transport.requests == []  # SAFE 저장은 보내지 않는다.
        ctx = SimpleNamespace(client=client, story=story)
        response = put(client, story, {**current_payload(saved), "requested_rating": "UNSAFE"}, "k")
        assert response.status_code == 200, response.text
        assert coordinator.wait(5)
        return ctx

    contexts = []
    yield run
    for client, coordinator in contexts:
        client.__exit__(None, None, None)
        coordinator.close()


def test_eligible_reply_applies_unsafe_without_db_ids_CREATE_US_08_AC02_AC04(jev):
    transport = FakeTransport()
    ctx = jev(transport)
    assert rating_view(ctx) == ("UNSAFE", "completed", None)

    [request] = transport.requests
    sent = json.loads(request.body)
    assert [c["id"] for c in sent["state"]["characters"]] == ["c1"]
    assert not _numbers(sent["state"])  # 스토리·인물·관계 DB ID 같은 숫자를 보내지 않는다.


def _numbers(value):
    if isinstance(value, dict):
        return [n for item in value.values() for n in _numbers(item)]
    if isinstance(value, list):
        return [n for item in value for n in _numbers(item)]
    return [value] if isinstance(value, int | float) else []


def test_underage_reply_downgrades_with_reason_CREATE_US_08_AC04_AC09(jev):
    ctx = jev(FakeTransport(c1_age_band="under_20"))
    assert rating_view(ctx) == (
        "SAFE",
        "completed",
        {"outcome": "ineligible", "reasons": ["underage_stated"]},
    )


def test_provider_error_is_outage_not_ineligible_CREATE_US_08_AC11(jev):
    transport = FakeTransport(lambda request: OSError("connection reset"))
    ctx = jev(transport)
    assert rating_view(ctx) == (
        "SAFE",
        "failed",
        {"outcome": "unavailable", "reasons": ["evaluator_failure"]},
    )
    assert len(transport.requests) == 1  # 재시도하지 않는다.


@pytest.mark.parametrize(
    "case", ["choice-contradicted", "choice-tied", "model-unhashable-object", "confidence-bool"]
)
def test_malformed_reply_fails_closed_and_keeps_safe_CREATE_US_08_AC04_AC11(jev, case):
    """잘못된 공급자 응답은 승인이 아니다. 적용 등급은 SAFE로 남고 실패(invalid_signals)다."""
    transport = FakeTransport(INVALID[case])
    ctx = jev(transport)
    assert rating_view(ctx) == (
        "SAFE",
        "failed",
        {"outcome": "unavailable", "reasons": ["invalid_signals"]},
    )
    assert len(transport.requests) == 1  # 재시도하지 않는다.
