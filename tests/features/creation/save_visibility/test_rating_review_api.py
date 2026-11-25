"""CREATE-US-08-AC02·AC14 실제 공통 저장(HTTP)과 PostgreSQL에서 검토 넘김 확인.

평가기는 가짜이며, 마지막 통과 기록은 테스트 전용 메모리 보관소다. 운영 앱(`create_app()`)은
조정기 없이 만들어져 검토를 넘기지 않는다.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from types import SimpleNamespace

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from app.db import session
from app.features.creation.save_visibility import rating_review
from app.features.creation.save_visibility.rating_review import (
    DatabaseReviewResults,
    RatingReviewCoordinator,
    build_snapshot,
)
from app.main import create_app
from tests.features.creation.save_visibility.review_fakes import (
    ELIGIBLE,
    INELIGIBLE,
    UNAVAILABLE,
    UNDETERMINED,
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
from tests.features.creation.speech_style.test_working_copy_basic import (
    VALID_BASIC,
    autosave,
    basic_url,
    finalize_payload,
)
from tests.features.creation.speech_style.test_working_copy_presets import image
from tests.features.creation.speech_style.test_working_copy_save import publish

WORLD = "수정하면 안 되는 세계관"


def _context(db, db_url, results=None):
    evaluator, store = FakeEvaluator(), MemoryLastPassStore()
    coordinator = RatingReviewCoordinator(evaluator, store, results=results)
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


BOUNDED_TIMEOUTS = {"statement_timeout": "5s", "lock_timeout": "3s"}


def _bounded_dsn(db_url):
    """앱 풀·결과 보관소 연결이 모두 서버 측 시간 제한을 갖는 테스트 전용 접속 정보."""
    options = " ".join(f"-c {name}={value}" for name, value in BOUNDED_TIMEOUTS.items())
    return make_conninfo(db_url, connect_timeout=5, options=options)


def assert_bounded(connection):
    for name, value in BOUNDED_TIMEOUTS.items():
        assert connection.execute(f"SHOW {name}").fetchone()[name] == value, name


@pytest.fixture
def ctx(request, db, db_url):
    """기본은 기존 조정기(결과를 DB에 쓰지 않음). `indirect`로 "persisted"를 주면
    CREATE-US-08-AC02·AC03·AC09~AC11 검토 결과를 DB에 쓰는 조정기(테스트에서만 주입)다.
    "persisted-bounded"는 같은 조정기에 앱·결과 연결 모두 statement/lock/connect 시간 제한을
    건다(동시 재전송 테스트가 막혀도 정리가 끝나도록).
    """
    mode = getattr(request, "param", None)
    if mode == "persisted-bounded":
        dsn = _bounded_dsn(db_url)
        for context in _context(db, dsn, DatabaseReviewResults(dsn)):
            # 앱 풀(TestClient 요청)과 결과 보관소가 실제로 제한된 연결을 쓰는지 확인한다.
            with session._pool.connection() as app_connection:
                assert_bounded(app_connection)
            results_dsn = context.coordinator._results._conninfo
            with psycopg.connect(results_dsn, row_factory=dict_row) as results_connection:
                assert_bounded(results_connection)
            yield context
        return
    results = DatabaseReviewResults(db_url) if mode == "persisted" else None
    yield from _context(db, db_url, results)


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


@pytest.mark.parametrize("ctx", ["persisted-bounded"], indirect=True)
def test_handoff_failure_keeps_receipt_immutable_under_concurrent_replay_CREATE_US_08_AC02_AC11(
    ctx, monkeypatch
):
    """커밋 뒤 넘김 실패 기록이 진행 중일 때 같은 요청키를 다른 HTTP 요청으로 재전송한다.

    요청키 영수증은 커밋 시점 응답(검토 중)으로 고정된다. 원래 PUT 응답·실패 기록 중 재전송·
    그 뒤 재전송이 모두 같은 영수증이고, 넘김 실패는 현재 검토 상태(조회)에만 남는다.
    """
    saved = published(ctx)
    before = unsafe_passed(ctx, saved)
    _set_world(ctx, saved)
    attempts = []

    def boom(*args):
        attempts.append(args)
        raise RuntimeError("넘김 실패")

    monkeypatch.setattr(ctx.coordinator._executor, "submit", boom)
    # 커밋 뒤 넘김 실패를 DB에 쓰기 직전에 멈춘다(결과 보관소 호출을 가로챈다).
    original_apply = ctx.coordinator._results.apply
    entered, release = threading.Event(), threading.Event()
    applies = []

    def paused_apply(*args, **kwargs):
        applies.append(args[3])
        entered.set()
        assert release.wait(10), "실패 기록 해제 대기 시간 초과"
        return original_apply(*args, **kwargs)

    monkeypatch.setattr(ctx.coordinator._results, "apply", paused_apply)
    calls_before = len(ctx.evaluator.calls)
    payload = {**current_payload(saved), "requested_rating": "UNSAFE"}
    with ThreadPoolExecutor(max_workers=2) as executor:
        try:
            pending = executor.submit(put, ctx.client, ctx.story, payload, "broken-submit")
            assert entered.wait(10), "커밋 뒤 실패 기록에 도달하지 않았다"
            # 원래 PUT이 실패 기록 중에 멈춘 사이 다른 HTTP 요청으로 같은 키를 재전송한다.
            replay = executor.submit(put, ctx.client, ctx.story, payload, "broken-submit")
            concurrent = replay.result(timeout=10)
            assert not pending.done()
        finally:
            release.set()
        result = pending.result(timeout=10)
    assert result.status_code == 200, result.text
    assert concurrent.status_code == 200, concurrent.text
    assert ctx.coordinator.wait(5)
    later = put(ctx.client, ctx.story, payload, "broken-submit")
    assert later.status_code == 200, later.text

    data = result.json()
    # 영수증은 커밋 시점 값(검토 중, 앞선 UNSAFE 유지)이고 세 응답이 같다.
    assert (data["effective_rating"], data["rating_review_status"]) == ("UNSAFE", "reviewing")
    assert data["rating_review_reason"] is None
    assert concurrent.json() == data
    assert later.json() == data
    # 실패는 부적격이 아니다. 현재 상태 조회에만 실패와 사유 코드가 보이고 saved_at은 같다.
    failure = {"outcome": "unavailable", "reasons": ["evaluator_failure"]}
    assert rating_view(ctx) == ("UNSAFE", "failed", failure)
    assert creation(ctx).json()["saved_at"] == data["saved_at"]
    assert datetime.fromisoformat(data["saved_at"]) == canonical_saved_at(ctx)
    row = rating_row(ctx.dsn, ctx.story)
    assert row["rating_requested_at"] > before["rating_requested_at"]
    # 한 번만 넘기려 했고 실패 기록도 한 번이며, 재전송으로 다시 넘기거나 평가하지 않는다.
    assert len(attempts) == 1
    assert applies == [rating_review.FAILURE]
    assert len(ctx.evaluator.calls) == calls_before


def _wait_blocked_by(db, holder_pid, deadline):
    """독립 연결에서 `holder_pid`에 막혀 행 잠금을 기다리는 백엔드를 찾을 때까지 확인한다.

    시간으로 순서를 맞추지 않는다. PostgreSQL이 실제로 막고 있음(pg_blocking_pids)을 관측할
    때까지 기한 안에서 조건을 다시 읽을 뿐이다.
    """
    while True:
        rows = db.execute(
            """SELECT pid, wait_event_type, query FROM pg_stat_activity
               WHERE datname = current_database() AND %s = ANY(pg_blocking_pids(pid))""",
            (holder_pid,),
        ).fetchall()
        if rows or time.monotonic() > deadline:
            return rows
        time.sleep(0.02)


@pytest.mark.parametrize("ctx", ["persisted-bounded"], indirect=True)
def test_replay_blocks_on_failure_write_transaction_and_keeps_receipt_CREATE_US_08_AC02_AC11(
    ctx, monkeypatch
):
    """커밋 뒤 넘김 실패를 실제로 UPDATE한 뒤 트랜잭션이 끝나기 전에 멈춘다(스토리 행 잠금 보유).

    같은 요청키 재전송은 PostgreSQL에서 그 작업자 연결에 막혀야 하고(pg_blocking_pids로 확인),
    해제 뒤에도 원래 응답과 같은 커밋 시점 영수증(검토 중)을 돌려준다. 조회는 실패를 보인다.
    """
    saved = published(ctx)
    before = unsafe_passed(ctx, saved)
    _set_world(ctx, saved)
    attempts = []

    def boom(*args):
        attempts.append(args)
        raise RuntimeError("넘김 실패")

    monkeypatch.setattr(ctx.coordinator._executor, "submit", boom)
    original_apply = rating_review.repository.apply_review_result
    entered, release = threading.Event(), threading.Event()
    holders, writes = [], []

    def paused_apply(connection, *args, **kwargs):
        applied = original_apply(connection, *args, **kwargs)
        writes.append((*args[2:5], applied))
        holders.append(connection.info.backend_pid)
        assert_bounded(connection)  # 잠금을 쥔 결과 연결에도 시간 제한이 걸려 있다.
        assert connection.info.transaction_status == psycopg.pq.TransactionStatus.INTRANS
        entered.set()
        assert release.wait(10), "실패 기록 트랜잭션 해제 대기 시간 초과"
        return applied

    monkeypatch.setattr(rating_review.repository, "apply_review_result", paused_apply)
    calls_before = len(ctx.evaluator.calls)
    payload = {**current_payload(saved), "requested_rating": "UNSAFE"}
    with ThreadPoolExecutor(max_workers=2) as executor:
        try:
            pending = executor.submit(put, ctx.client, ctx.story, payload, "held-failure")
            assert entered.wait(10), "실패 기록 UPDATE 뒤 멈춤 지점에 도달하지 않았다"
            # 작업자 연결은 아직 트랜잭션 안이고 UPDATE는 커밋 전이라 다른 연결에는 안 보인다.
            assert rating_row(ctx.dsn, ctx.story)["rating_review_status"] == "reviewing"
            replay = executor.submit(put, ctx.client, ctx.story, payload, "held-failure")
            with psycopg.connect(
                ctx.dsn, autocommit=True, row_factory=psycopg.rows.dict_row
            ) as observer:
                assert_bounded(observer)
                blocked = _wait_blocked_by(observer, holders[0], time.monotonic() + 10)
            assert len(blocked) == 1, "재전송이 작업자 연결에 막히지 않았다"
            assert blocked[0]["wait_event_type"] == "Lock"
            assert "FOR UPDATE" in blocked[0]["query"]
            assert not pending.done() and not replay.done()
        finally:
            release.set()
        result = pending.result(timeout=10)
        concurrent = replay.result(timeout=10)
    assert result.status_code == 200, result.text
    assert concurrent.status_code == 200, concurrent.text
    assert ctx.coordinator.wait(5)
    later = put(ctx.client, ctx.story, payload, "held-failure")
    assert later.status_code == 200, later.text

    data = result.json()
    # 영수증은 커밋 시점 값(검토 중, 앞선 UNSAFE 유지)이고 세 응답이 같다.
    assert (data["effective_rating"], data["rating_review_status"]) == ("UNSAFE", "reviewing")
    assert data["rating_review_reason"] is None
    assert concurrent.json() == data
    assert later.json() == data
    failure = {"outcome": "unavailable", "reasons": ["evaluator_failure"]}
    assert rating_view(ctx) == ("UNSAFE", "failed", failure)
    assert creation(ctx).json()["saved_at"] == data["saved_at"]
    assert datetime.fromisoformat(data["saved_at"]) == canonical_saved_at(ctx)
    assert rating_row(ctx.dsn, ctx.story)["rating_requested_at"] > before["rating_requested_at"]
    # 실제 UPDATE는 한 번(실패 기록)만 쓰였고, 재전송으로 다시 넘기거나 평가하지 않는다.
    assert writes == [(None, "failed", rating_review.FAILURE.reason, True)]
    assert len(attempts) == 1
    assert len(ctx.evaluator.calls) == calls_before


def test_default_app_has_no_review_coordinator_CREATE_US_08_AC02():
    from app.main import app

    assert app.state.rating_review is None


@pytest.mark.parametrize("ctx", ["persisted"], indirect=True)
def test_first_pass_changes_persisted_effective_rating_CREATE_US_08_AC02_AC03(ctx):
    saved = published(ctx)
    ctx.evaluator.gate.clear()
    try:
        result = save(ctx, saved, "first-pass", requested_rating="UNSAFE")
        assert result.status_code == 200, result.text
        assert ctx.evaluator.started.wait(5)
        pending = rating_row(ctx.dsn, ctx.story)
        assert (pending["requested_rating"], pending["effective_rating"]) == ("UNSAFE", "SAFE")
    finally:
        ctx.evaluator.gate.set()
    assert reviewed(ctx) == [expected(ctx)]

    complete = rating_row(ctx.dsn, ctx.story)
    assert complete["effective_rating"] == "UNSAFE"
    assert complete["rating_review_status"] == "completed"
    assert pending["rating_review_status"] == "reviewing"
    assert complete["rating_requested_at"] == pending["rating_requested_at"]
    assert complete["rating_requested_at"] is not None

    story_id = ctx.story["id"]
    readback = ctx.client.get(f"/api/v1/creation/stories/{story_id}/creation")
    assert readback.status_code == 200, readback.text
    assert (readback.json()["effective_rating"], readback.json()["rating_review_status"]) == (
        "UNSAFE",
        "completed",
    )


# --- CREATE-US-08-AC03·AC09~AC11 검토 결과 저장(결과 보관소를 주입한 조정기, 테스트 전용) ---

RAW = "공급자 원문: 이 문장은 응답·DB에 나오면 안 된다"


def creation(ctx, user_id=OWNER_ID):
    return ctx.client.get(
        f"/api/v1/creation/stories/{ctx.story['id']}/creation",
        headers={"X-User-Id": str(user_id)},
    )


def rating_view(ctx):
    data = creation(ctx).json()
    keys = ("effective_rating", "rating_review_status", "rating_review_reason")
    return tuple(data[k] for k in keys)


def unsafe_passed(ctx, saved, key="first-pass"):
    """처음 UNSAFE 확정 저장이 적격으로 통과해 적용 등급이 UNSAFE가 된 스토리."""
    ctx.evaluator.verdict = ELIGIBLE
    assert save(ctx, saved, key, requested_rating="UNSAFE").status_code == 200
    assert ctx.coordinator.wait(5)
    assert rating_view(ctx) == ("UNSAFE", "completed", None)
    return rating_row(ctx.dsn, ctx.story)


@pytest.mark.parametrize(
    ("verdict", "reason"),
    [
        (INELIGIBLE, {"outcome": "ineligible", "reasons": ["underage_stated"]}),
        (UNDETERMINED, {"outcome": "undetermined", "reasons": ["age_undetermined"]}),
    ],
)
@pytest.mark.parametrize("ctx", ["persisted"], indirect=True)
def test_ineligible_or_undetermined_downgrades_to_safe_with_reason_CREATE_US_08_AC09_AC10(
    ctx, verdict, reason
):
    saved = published(ctx)
    before = unsafe_passed(ctx, saved)
    _set_world(ctx, saved)
    ctx.evaluator.verdict = verdict
    ctx.evaluator.gate.clear()
    try:
        result = save(ctx, saved, "recheck", requested_rating="UNSAFE")
        assert result.status_code == 200, result.text
        # 재검토 중에는 앞선 UNSAFE를 유지하고 사유는 비운다.
        data = result.json()
        assert (data["effective_rating"], data["rating_review_status"]) == ("UNSAFE", "reviewing")
        assert data["rating_review_reason"] is None
        assert ctx.evaluator.started.wait(5)
        assert rating_view(ctx) == ("UNSAFE", "reviewing", None)
    finally:
        ctx.evaluator.gate.set()
    assert reviewed(ctx)[-1] == expected(ctx, world="새 세계관")

    assert rating_view(ctx) == ("SAFE", "completed", reason)
    row = rating_row(ctx.dsn, ctx.story)
    assert row["requested_rating"] == "UNSAFE"  # 제작자 선택은 바꾸지 않는다.
    assert row["rating_requested_at"] > before["rating_requested_at"]


def _raises(_snapshot):
    return RuntimeError(RAW)


@pytest.mark.parametrize(
    ("verdict", "reason"),
    [
        (_raises, {"outcome": "unavailable", "reasons": ["evaluator_failure"]}),
        (UNAVAILABLE, {"outcome": "unavailable", "reasons": ["evaluator_failure"]}),
        (RAW, {"outcome": "unavailable", "reasons": ["invalid_signals"]}),
    ],
    ids=["exception", "unavailable", "raw-text"],
)
@pytest.mark.parametrize("ctx", ["persisted"], indirect=True)
def test_outage_keeps_previous_effective_rating_CREATE_US_08_AC11(ctx, verdict, reason):
    saved = published(ctx)
    unsafe_passed(ctx, saved)
    _set_world(ctx, saved)
    ctx.evaluator.verdict = verdict
    assert save(ctx, saved, "recheck", requested_rating="UNSAFE").status_code == 200
    assert len(reviewed(ctx)) == 2

    # 실패는 부적격이 아니다. 앞선 UNSAFE를 유지하고 허용 목록 코드만 남긴다.
    assert rating_view(ctx) == ("UNSAFE", "failed", reason)
    row = rating_row(ctx.dsn, ctx.story)
    assert RAW not in (row["rating_review_reason"] or "")
    assert RAW not in creation(ctx).text


@pytest.mark.parametrize("ctx", ["persisted"], indirect=True)
def test_stale_in_flight_result_is_not_written_after_newer_save_CREATE_US_08_AC02_AC03(ctx):
    saved = published(ctx)
    first = expected(ctx)
    # 앞선 내용은 적격(쓰이면 UNSAFE로 오른다), 나중 내용은 부적격이다.
    ctx.evaluator.verdict = lambda snapshot: ELIGIBLE if snapshot == first else INELIGIBLE
    ctx.evaluator.gate.clear()
    try:
        assert save(ctx, saved, "older", requested_rating="UNSAFE").status_code == 200
        assert ctx.evaluator.started.wait(5)
        older = rating_row(ctx.dsn, ctx.story)["rating_requested_at"]
        _set_world(ctx, saved)
        assert save(ctx, saved, "newer", requested_rating="UNSAFE").status_code == 200
        newer = rating_row(ctx.dsn, ctx.story)["rating_requested_at"]
        assert newer > older
    finally:
        ctx.evaluator.gate.set()
    assert reviewed(ctx) == [first, expected(ctx, world="새 세계관")]

    # 앞선(평가 중이던) 적격 결과는 요청 표가 달라 버려지고, 마지막 요청의 결과만 남는다.
    row = rating_row(ctx.dsn, ctx.story)
    assert row["rating_requested_at"] == newer
    assert rating_view(ctx) == (
        "SAFE",
        "completed",
        {"outcome": "ineligible", "reasons": ["underage_stated"]},
    )


@pytest.mark.parametrize("ctx", ["persisted"], indirect=True)
def test_safe_save_invalidates_in_flight_review_without_reusing_ticket_CREATE_US_08_AC02(ctx):
    saved = published(ctx)
    ctx.evaluator.verdict = ELIGIBLE
    ctx.evaluator.gate.clear()
    try:
        assert save(ctx, saved, "unsafe", requested_rating="UNSAFE").status_code == 200
        assert ctx.evaluator.started.wait(5)
        ticket = rating_row(ctx.dsn, ctx.story)["rating_requested_at"]
        assert save(ctx, saved, "safe", requested_rating="SAFE").status_code == 200
        assert rating_view(ctx) == ("SAFE", "not_reviewed", None)
        # 마지막 요청 시각은 남긴다(다음 요청 표의 하한).
        assert rating_row(ctx.dsn, ctx.story)["rating_requested_at"] == ticket
    finally:
        ctx.evaluator.gate.set()
    assert ctx.coordinator.wait(5)
    # 평가 중이던 적격 결과는 SAFE 저장 뒤라 쓰이지 않는다.
    assert rating_view(ctx) == ("SAFE", "not_reviewed", None)

    # 같은 내용을 다시 UNSAFE로 저장하면 앞선 것보다 큰 새 요청 표로 다시 검토한다.
    assert save(ctx, saved, "unsafe-again", requested_rating="UNSAFE").status_code == 200
    assert ctx.coordinator.wait(5)
    row = rating_row(ctx.dsn, ctx.story)
    assert row["rating_requested_at"] > ticket
    assert rating_view(ctx) == ("UNSAFE", "completed", None)


@pytest.mark.parametrize(
    "stored",
    [
        "검토 완료 사유",  # 예전 자유 텍스트
        "ineligible:",
        "eligible:",
        "ineligible:underage_stated,unknown_code",
        "undetermined:underage_stated",  # 판정과 사유가 맞지 않는다
        RAW,
    ],
)
def test_unknown_stored_reason_is_not_exposed_CREATE_US_08_AC09(ctx, stored):
    ctx.db.execute(
        """UPDATE stories SET requested_rating='UNSAFE', rating_review_status='completed',
                  rating_review_reason=%s WHERE id=%s""",
        (stored, ctx.story["id"]),
    )
    response = creation(ctx)
    assert response.status_code == 200, response.text
    assert response.json()["rating_review_reason"] is None
    assert stored not in response.text


def test_reason_is_owner_only_CREATE_US_08_AC09(ctx):
    ctx.db.execute(
        "UPDATE stories SET rating_review_reason='ineligible:underage_stated' WHERE id=%s",
        (ctx.story["id"],),
    )
    assert creation(ctx).json()["rating_review_reason"] == {
        "outcome": "ineligible",
        "reasons": ["underage_stated"],
    }
    other = creation(ctx, user_id=OWNER_ID + 999)
    assert other.status_code == 404
    assert "underage_stated" not in other.text


# --- CREATE-US-08-AC02·AC03 × CREATE-US-07-AC09: 검토 메타데이터만 바뀌면 saved_at 유지 ---
# 검토 대기·결과 쓰기는 저장본 편집이 아니다. saved_at이 움직이면 그 사이 받은 자동 저장 영수증이
# 거짓 409로 확정되지 않고, 확정 저장 응답의 saved_at도 정본과 어긋난다.


def canonical_saved_at(ctx):
    return ctx.db.execute(
        "SELECT saved_at FROM stories WHERE id=%s", (ctx.story["id"],)
    ).fetchone()["saved_at"]


def assert_matches_canonical(ctx, result):
    """확정 저장 응답이 정본 조회와 정확히 같다(saved_at·등급 필드 포함)."""
    data = result.json()
    assert datetime.fromisoformat(data["saved_at"]) == canonical_saved_at(ctx)
    readback = creation(ctx).json()
    for key in ("saved_at", "requested_rating", "effective_rating", "rating_review_status"):
        assert data[key] == readback[key], key
    assert data["rating_review_reason"] == readback["rating_review_reason"]


@pytest.mark.parametrize("ctx", ["persisted"], indirect=True)
def test_pending_final_save_response_saved_at_is_canonical_and_kept_CREATE_US_08_AC02_AC03(ctx):
    saved = published(ctx)
    ctx.evaluator.verdict = ELIGIBLE
    ctx.evaluator.gate.clear()
    try:
        result = save(ctx, saved, "unsafe", requested_rating="UNSAFE")
        assert result.status_code == 200, result.text
        assert result.json()["rating_review_status"] == "reviewing"
        assert ctx.evaluator.started.wait(5)
        assert_matches_canonical(ctx, result)
        pending = canonical_saved_at(ctx)
    finally:
        ctx.evaluator.gate.set()
    assert ctx.coordinator.wait(5)

    # 검토 완료(적용 등급·상태 쓰기)는 saved_at을 움직이지 않는다.
    assert rating_view(ctx) == ("UNSAFE", "completed", None)
    assert canonical_saved_at(ctx) == pending
    assert creation(ctx).json()["saved_at"] == result.json()["saved_at"]


@pytest.mark.parametrize("ctx", ["persisted"], indirect=True)
def test_safe_final_save_response_saved_at_is_canonical_CREATE_US_08_AC02(ctx):
    saved = published(ctx)
    # 이미 SAFE·not_reviewed인 스토리의 SAFE 확정 저장(등급 메타데이터 변화 없음).
    same = save(ctx, saved, "safe-same", requested_rating="SAFE")
    assert same.status_code == 200, same.text
    assert_matches_canonical(ctx, same)

    # UNSAFE 통과 뒤 SAFE 확정 저장(적용 등급·상태가 실제로 바뀜).
    unsafe_passed(ctx, saved)
    back = save(ctx, saved, "safe-back", requested_rating="SAFE")
    assert back.status_code == 200, back.text
    assert rating_view(ctx) == ("SAFE", "not_reviewed", None)
    assert_matches_canonical(ctx, back)


def _common_autosave(ctx, saved, key):
    auto = autosave(ctx.client, ctx.story, saved, key, basic=VALID_BASIC)
    assert auto["finalize_auto_request_id"] == key


def _photo_autosave(ctx, saved, key):
    result = ctx.client.put(
        f"{basic_url(ctx.story)}/images",
        json={"kind": "temporary", "mode": "auto", "story_image_ids": [image(ctx.dsn)]},
        headers={"Idempotency-Key": key},
    )
    assert result.status_code == 200, result.text
    assert result.json()["finalize_auto_request_id"] == key


@pytest.mark.parametrize("make_autosave", [_common_autosave, _photo_autosave])
@pytest.mark.parametrize("ctx", ["persisted"], indirect=True)
def test_autosave_receipt_finalizes_after_review_completion_CREATE_US_07_AC09_US_08_AC03(
    ctx, make_autosave
):
    saved = published(ctx)
    ctx.evaluator.verdict = ELIGIBLE
    ctx.evaluator.gate.clear()
    try:
        assert save(ctx, saved, "unsafe", requested_rating="UNSAFE").status_code == 200
        assert ctx.evaluator.started.wait(5)
        make_autosave(ctx, saved, "auto-during-review")
    finally:
        ctx.evaluator.gate.set()
    assert ctx.coordinator.wait(5)
    assert rating_view(ctx)[:2] == ("UNSAFE", "completed")

    final = put(ctx.client, ctx.story, finalize_payload(saved, "auto-during-review"), "final")
    assert final.status_code == 200, final.text
    assert final.json()["finalize_auto_request_id"] is None
    assert_matches_canonical(ctx, final)
