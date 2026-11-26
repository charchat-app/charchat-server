"""CHAT-US-13 테스트 공통 준비: 저장 스토리·세션과 결정적인 가짜 생성(외부 호출 없음)."""

import pytest
from model_change_helpers import RecordingProvider, seed_story, start_session

from app.core.config import settings
from app.features.conversation.messaging import providers, worker


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
