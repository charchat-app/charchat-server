"""CHAT-US-08 장면 조회·이용자 직접 수정 테스트 공통 준비(외부 호출 없음).

이 폴더의 순수 규칙·배포 SQL 테스트는 아래 픽스처를 쓰지 않으며 영향도 받지 않는다.
"""

import pytest
from scene_helpers import RecordingProvider, ScriptedJudge, seed_story, start_session

from app.core.config import settings
from app.features.conversation.messaging import providers, worker
from app.features.conversation.scene import judge as scene_judge


@pytest.fixture(autouse=True)
def dev_user_header(dev_user_header_enabled):
    """BASE-03: 이 폴더 테스트는 개발용 X-User-Id 헤더로 사용자를 정한다(tests/conftest.py)."""


@pytest.fixture(autouse=True)
def inline_worker(monkeypatch):
    monkeypatch.setattr(settings, "chat_generation_worker", "inline")
    monkeypatch.setattr(settings, "chat_fake_chunk_delay_ms", 0)
    monkeypatch.setattr(settings, "chat_fake_fail_mode", "none")
    monkeypatch.setattr(settings, "chat_llm_provider", "fake")
    monkeypatch.setattr(settings, "chat_default_model_name", "루나")
    monkeypatch.setattr(settings, "chat_stream_flush_ms", 0)


@pytest.fixture
def recorder(monkeypatch):
    provider = RecordingProvider()
    monkeypatch.setattr(providers, "get_provider", lambda: provider)
    return provider


@pytest.fixture
def judge():
    """장면 판단 결과를 정한다. 상위 conftest의 '바뀐 것 없음' 가짜를 이 테스트 동안 바꾼다."""
    original = scene_judge.get_judge_provider
    provider = ScriptedJudge()
    scene_judge.get_judge_provider = lambda: provider
    yield provider
    scene_judge.get_judge_provider = original


@pytest.fixture
def deferred(monkeypatch):
    """생성 작업을 바로 실행하지 않고 모아 둔다. 테스트가 원하는 시점에 run()으로 실행한다."""

    class Deferred:
        def __init__(self):
            self.jobs = []

        def run(self):
            jobs, self.jobs = self.jobs, []
            for job, args in jobs:
                job(*args)

    queue = Deferred()
    monkeypatch.setattr(worker, "submit", lambda job, *args: queue.jobs.append((job, args)))
    return queue


@pytest.fixture
def saved_story(db):
    return seed_story(db)


@pytest.fixture
def session(client, saved_story):
    story, intro = saved_story
    return start_session(client, story, intro)
