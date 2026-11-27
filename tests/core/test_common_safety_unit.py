"""CREATE-US-08-AC15 공통 안전 경계 단위 테스트(DB 없음)."""

import logging

import pytest

from app.core.common_safety import CommonSafety, SafetySurface, is_clear
from tests.common_safety_fakes import BLOCK_MARKER, FakeGuard

SURFACE = SafetySurface(
    story_id=1,
    title="t",
    subtitle="s",
    world_description="w",
    tags=(),
    characters=(),
    relationships=(),
    intros=(),
)


def test_only_clear_member_passes_CREATE_US_08_AC15():
    assert is_clear(FakeGuard(CommonSafety.CLEAR), SURFACE)


@pytest.mark.parametrize(
    "outcome",
    [CommonSafety.BLOCKED, CommonSafety.UNAVAILABLE, "clear", None, True, object()],
)
def test_blocked_unavailable_and_invalid_fail_closed_CREATE_US_08_AC15(outcome):
    assert not is_clear(FakeGuard(outcome), SURFACE)


def test_evaluator_error_fails_closed_without_raw_log_CREATE_US_08_AC15(caplog):
    caplog.set_level(logging.DEBUG)
    assert not is_clear(FakeGuard(RuntimeError(BLOCK_MARKER)), SURFACE)
    assert BLOCK_MARKER not in caplog.text
    assert "RuntimeError" in caplog.text


def test_default_app_has_no_guard_CREATE_US_08_AC15():
    from app.main import create_app

    assert create_app().state.common_safety is None
