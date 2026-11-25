"""CHAT-US-03 테스트 공통 준비: 결정적인 가짜 생성(외부 호출 없음)."""

import pytest
from resume_helpers import seed_story

from app.core.config import settings


@pytest.fixture(autouse=True)
def inline_worker(monkeypatch):
    monkeypatch.setattr(settings, "chat_generation_worker", "inline")
    monkeypatch.setattr(settings, "chat_fake_chunk_delay_ms", 0)
    monkeypatch.setattr(settings, "chat_fake_fail_mode", "none")
    monkeypatch.setattr(settings, "chat_llm_provider", "fake")
    monkeypatch.setattr(settings, "chat_default_model_name", "루나")


@pytest.fixture
def story(db):
    return seed_story(db)
