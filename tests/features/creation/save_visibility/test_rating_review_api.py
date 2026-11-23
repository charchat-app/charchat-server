"""CREATE-US-08-AC02·AC14 실제 공통 저장(HTTP)과 PostgreSQL에서 검토 넘김 확인.

평가기는 가짜이며, 마지막 통과 기록은 테스트 전용 메모리 보관소다. 운영 앱(`create_app()`)은
조정기 없이 만들어져 검토를 넘기지 않는다.
"""

from types import SimpleNamespace

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.features.creation.save_visibility import rating_review
from app.features.creation.save_visibility.rating_review import (
    RatingReviewCoordinator,
    build_snapshot,
)
from app.main import create_app
from tests.features.creation.save_visibility.review_fakes import (
    ELIGIBLE,
    INELIGIBLE,
    FakeEvaluator,
    MemoryLastPassStore,
)
from tests.features.creation.speech_style.conftest import OWNER_ID, create_story
from tests.features.creation.speech_style.test_common_save_api import body, current_payload, put
from tests.features.creation.speech_style.test_common_save_rating import (
    rating_finalize,
    rating_only_stage,
    rating_row,
)
from tests.features.creation.speech_style.test_working_copy_save import publish

WORLD = "수정하면 안 되는 세계관"


@pytest.fixture
def ctx(db, db_url):
    evaluator, store = FakeEvaluator(), MemoryLastPassStore()
    coordinator = RatingReviewCoordinator(evaluator, store)
    with TestClient(create_app(db_url, rating_review=coordinator)) as client:
        client.headers["X-User-Id"] = str(OWNER_ID)
        yield SimpleNamespace(
            client=client,
            story=create_story(db),
            dsn=db_url,
            db=db,
            evaluator=evaluator,
            store=store,
            coordinator=coordinator,
        )
    evaluator.gate.set()
    coordinator.close()


def published(ctx):
    """비공개·SAFE로 저장한 스토리. SAFE 저장이라 검토를 넘기지 않는다."""
    saved = publish(ctx.client, ctx.story, ctx.dsn)
    assert ctx.coordinator.wait(5)
    assert ctx.evaluator.calls == []
    return saved


def save(ctx, saved, key, **extra):
    return put(ctx.client, ctx.story, {**current_payload(saved), **extra}, key)


def character_id(ctx):
    return ctx.db.execute(
        "SELECT id FROM characters WHERE story_id=%s", (ctx.story["id"],)
    ).fetchone()["id"]


def expected(ctx, world=WORLD, detail="원래 상세", relations=()):
    name = ctx.db.execute(
        "SELECT name FROM characters WHERE id=%s", (character_id(ctx),)
    ).fetchone()["name"]
    people = [{"id": character_id(ctx), "name": name, "description": detail}]
    return build_snapshot(world, people, list(relations))


def reviewed(ctx):
    assert ctx.coordinator.wait(5)
    return [snapshot for _, snapshot in ctx.evaluator.calls]


def test_unsafe_save_hands_off_after_commit_without_waiting_CREATE_US_08_AC02(ctx):
    saved = published(ctx)
    seen = []

    def look(story_id, _snapshot):
        # 다른 연결에서 읽어 커밋된 뒤 넘겼는지 확인한다.
        with psycopg.connect(ctx.dsn) as connection:
            seen.append(
                connection.execute(
                    "SELECT requested_rating, save_state FROM stories WHERE id=%s", (story_id,)
                ).fetchone()
            )

    ctx.evaluator.on_evaluate = look
    ctx.evaluator.gate.clear()
    result = save(ctx, saved, "unsafe", requested_rating="UNSAFE")
    # 평가기가 막혀 있어도 응답은 바로 온다. 적용 등급·검토 상태는 바꾸지 않는다.
    assert result.status_code == 200, result.text
    data = result.json()
    assert (data["requested_rating"], data["effective_rating"]) == ("UNSAFE", "SAFE")
    assert data["rating_review_status"] == "not_reviewed"
    assert ctx.evaluator.started.wait(5)
    assert ctx.store.passes == {}
    ctx.evaluator.gate.set()
    assert reviewed(ctx) == [expected(ctx)]
    assert seen == [("UNSAFE", "private")]
    assert ctx.store.get(ctx.story["id"]) == expected(ctx)
    row = rating_row(ctx.dsn, ctx.story)
    assert row["effective_rating"] == "SAFE"
    assert row["rating_review_status"] == "not_reviewed"
    assert row["rating_review_reason"] is None and row["rating_requested_at"] is None


def _pending_temporary(ctx, saved):
    staged = save(ctx, saved, "stage", kind="temporary", requested_rating="UNSAFE")
    assert staged.status_code == 200
    return save(ctx, saved, "commit")


def _canonical_unsafe(ctx, saved):
    ctx.db.execute("UPDATE stories SET requested_rating='UNSAFE' WHERE id=%s", (ctx.story["id"],))
    return save(ctx, saved, "commit")


def _public(ctx, saved):
    return save(ctx, saved, "commit", save_state="public", requested_rating="UNSAFE")


def _rating_finalize(ctx, saved):
    staged = put(ctx.client, ctx.story, rating_only_stage(saved), "stage")
    assert staged.status_code == 200
    finalize = rating_finalize(saved, staged.json()["working_copy_saved_at"])
    return put(ctx.client, ctx.story, finalize, "commit")


@pytest.mark.parametrize(
    "arrange", [_pending_temporary, _canonical_unsafe, _public, _rating_finalize]
)
def test_final_save_with_persisted_unsafe_hands_off_once_CREATE_US_08_AC02(ctx, arrange):
    saved = published(ctx)
    result = arrange(ctx, saved)
    assert result.status_code == 200, result.text
    assert result.json()["requested_rating"] == "UNSAFE"
    assert reviewed(ctx) == [expected(ctx)]


def _temporary(ctx, saved):
    return save(ctx, saved, "x", kind="temporary", requested_rating="UNSAFE")


def _auto(ctx, saved):
    return save(ctx, saved, "x", mode="auto", requested_rating="UNSAFE")


def _safe(ctx, saved):
    return save(ctx, saved, "x", requested_rating="SAFE")


def _rejected(ctx, saved):
    # 완성 검사 실패로 전체가 되돌아간다.
    return save(ctx, saved, "x", intros=[], requested_rating="UNSAFE")


def _illegal_transition(ctx, saved):
    return save(ctx, saved, "x", save_state="draft", requested_rating="UNSAFE")


@pytest.mark.parametrize(
    ("arrange", "status"),
    [(_temporary, 200), (_auto, 200), (_safe, 200), (_rejected, 422), (_illegal_transition, 409)],
)
def test_no_handoff_without_committed_unsafe_final_save_CREATE_US_08_AC02(ctx, arrange, status):
    saved = published(ctx)
    assert arrange(ctx, saved).status_code == status
    assert reviewed(ctx) == []


def test_draft_unsafe_save_does_not_hand_off_CREATE_US_08_AC02(ctx):
    result = put(ctx.client, ctx.story, {**body("draft"), "requested_rating": "UNSAFE"}, "draft")
    assert result.status_code == 200, result.text
    assert result.json()["requested_rating"] == "UNSAFE"
    assert reviewed(ctx) == []


def test_idempotent_replay_does_not_hand_off_again_CREATE_US_08_AC02(ctx):
    saved = published(ctx)
    ctx.evaluator.verdict = INELIGIBLE  # 통과 기록 없이도 재전송은 넘기지 않는다.
    payload = {**current_payload(saved), "requested_rating": "UNSAFE"}
    first = put(ctx.client, ctx.story, payload, "same")
    assert first.status_code == 200
    assert reviewed(ctx) == [expected(ctx)]
    replay = put(ctx.client, ctx.story, payload, "same")
    assert replay.json() == first.json()
    assert len(reviewed(ctx)) == 1


def _set_world(ctx, saved):
    ctx.db.execute(
        "UPDATE stories SET world_description='새 세계관' WHERE id=%s", (ctx.story["id"],)
    )
    return {}


def _set_detail(ctx, saved):
    ctx.db.execute("UPDATE characters SET description='새 상세' WHERE id=%s", (character_id(ctx),))
    return {}


def _relation(label):
    def arrange(ctx, saved):
        link = {
            "client_key": "r",
            "source_kind": "character",
            "source_character_id": character_id(ctx),
            "target_kind": "user",
            "target_character_id": None,
            "label": label,
        }
        return {"relations": [link]}

    arrange.__name__ = f"_relation_{label!r}"
    return arrange


def _intro(ctx, saved):
    intros = current_payload(saved)["intros"]
    intros[0]["messages"][0]["body"] = "*눈이 내린다.*"
    return {"intros": intros}


def _fewshot(ctx, saved):
    fewshots = current_payload(saved)["fewshots"]
    fewshots[0]["messages"][0]["body"] = "새 예시"
    return {"fewshots": fewshots}


def _custom_style(ctx, saved):
    expression = current_payload(saved)["expression"]
    expression["custom_style"] = "새 문체"
    return {"expression": expression}


def _affinity(ctx, saved):
    item = {
        "source_character_id": character_id(ctx),
        "target_kind": "user",
        "target_character_id": None,
        "value": "30",
    }
    return {"affinities": [item]}


def _title_subtitle(ctx, saved):
    ctx.db.execute(
        "UPDATE stories SET title='새 제목', subtitle='새 소개' WHERE id=%s", (ctx.story["id"],)
    )
    return {}


def _hashtag(ctx, saved):
    tag = ctx.db.execute("INSERT INTO tags(name) VALUES ('태그') RETURNING id").fetchone()
    ctx.db.execute(
        "INSERT INTO story_tags(story_id,tag_id) VALUES (%s,%s)", (ctx.story["id"], tag["id"])
    )
    return {}


@pytest.mark.parametrize(
    ("change", "included"),
    [
        (_set_world, True),
        (_set_detail, True),
        (_relation("소꿉친구"), True),
        (_relation("   "), False),
        (_relation(None), False),
        (_intro, False),
        (_fewshot, False),
        (_custom_style, False),
        (_affinity, False),
        (_title_subtitle, False),
        (_hashtag, False),
    ],
)
def test_only_reviewed_committed_content_triggers_new_review_CREATE_US_08_AC14(
    ctx, change, included
):
    saved = published(ctx)
    first = save(ctx, saved, "first", requested_rating="UNSAFE")
    assert first.status_code == 200
    assert reviewed(ctx) == [expected(ctx)]
    second = save(ctx, first.json(), "second", **change(ctx, first.json()))
    assert second.status_code == 200, second.text
    calls = reviewed(ctx)
    assert len(calls) == (2 if included else 1)
    assert ctx.store.get(ctx.story["id"]) == calls[-1]


def test_failed_review_keeps_older_pass_and_is_retried_on_next_save_CREATE_US_08_AC14(ctx):
    saved = published(ctx)
    first = save(ctx, saved, "first", requested_rating="UNSAFE")
    assert reviewed(ctx) == [expected(ctx)]
    _set_world(ctx, saved)
    ctx.evaluator.verdict = RuntimeError("공급자 오류")
    failed = save(ctx, first.json(), "second")
    # 검토가 실패해도 저장은 성공한다.
    assert failed.status_code == 200, failed.text
    assert len(reviewed(ctx)) == 2
    assert ctx.store.get(ctx.story["id"]) == expected(ctx)
    ctx.evaluator.verdict = ELIGIBLE
    again = save(ctx, first.json(), "third")
    assert again.status_code == 200
    assert reviewed(ctx)[1:] == [expected(ctx, world="새 세계관")] * 2
    assert ctx.store.get(ctx.story["id"]) == expected(ctx, world="새 세계관")


def test_same_active_snapshot_is_reviewed_once_CREATE_US_08_AC14(ctx):
    saved = published(ctx)
    ctx.evaluator.gate.clear()
    first = save(ctx, saved, "first", requested_rating="UNSAFE")
    assert ctx.evaluator.started.wait(5)
    # 내용은 같고 요청 원문(키·preserve_scene)만 다른 저장.
    assert save(ctx, first.json(), "second", preserve_scene=True).status_code == 200
    ctx.evaluator.gate.set()
    assert reviewed(ctx) == [expected(ctx)]
    assert ctx.store.history == [(ctx.story["id"], expected(ctx))]


def test_superseded_review_does_not_become_last_pass_CREATE_US_08_AC14(ctx):
    saved = published(ctx)
    ctx.evaluator.gate.clear()
    first = save(ctx, saved, "first", requested_rating="UNSAFE")
    assert ctx.evaluator.started.wait(5)
    _set_world(ctx, saved)
    assert save(ctx, first.json(), "second").status_code == 200
    ctx.evaluator.gate.set()
    newer = expected(ctx, world="새 세계관")
    assert reviewed(ctx) == [expected(ctx), newer]
    assert ctx.store.history == [(ctx.story["id"], newer)]


def _broken_submit(ctx, monkeypatch):
    def boom(*_args):
        raise RuntimeError("넘김 실패")

    monkeypatch.setattr(ctx.coordinator._executor, "submit", boom)


def _broken_capture(ctx, monkeypatch):
    def boom(connection, _story_id):
        connection.execute("SELECT 1 / 0")

    monkeypatch.setattr(rating_review, "capture_snapshot", boom)


@pytest.mark.parametrize("replacement", ["SAFE", "UNSAFE"])
def test_superseding_save_invalidates_inflight_review_CREATE_US_08_AC02(
    ctx, monkeypatch, replacement
):
    saved = published(ctx)
    ctx.evaluator.gate.clear()
    first = save(ctx, saved, "first", requested_rating="UNSAFE")
    assert first.status_code == 200
    assert ctx.evaluator.started.wait(5)
    if replacement == "UNSAFE":
        _broken_capture(ctx, monkeypatch)
    second = save(ctx, first.json(), "newer", requested_rating=replacement)
    assert second.status_code == 200, second.text
    assert rating_row(ctx.dsn, ctx.story)["requested_rating"] == replacement
    ctx.evaluator.gate.set()
    assert reviewed(ctx) == [expected(ctx)]
    assert ctx.store.history == []


@pytest.mark.parametrize("breakage", [_broken_submit, _broken_capture])
def test_handoff_error_does_not_fail_committed_save_CREATE_US_08_AC02(ctx, monkeypatch, breakage):
    saved = published(ctx)
    breakage(ctx, monkeypatch)
    result = save(ctx, saved, "unsafe", requested_rating="UNSAFE")
    assert result.status_code == 200, result.text
    assert rating_row(ctx.dsn, ctx.story)["requested_rating"] == "UNSAFE"
    assert reviewed(ctx) == []
    assert ctx.store.passes == {}


def test_default_app_has_no_review_coordinator_CREATE_US_08_AC02():
    from app.main import app

    assert app.state.rating_review is None
