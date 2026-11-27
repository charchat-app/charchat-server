"""CREATE-US-09-AC07: 확정 변경 집계 및 분류 결과 반영."""

from threading import Event

import psycopg

from app.features.creation.category_classification import worker
from app.features.creation.category_classification.worker import CycleReport, run_cycle, run_hourly


def seed(db, *, state="private", pending=True, deleted=False):
    story_id = db.execute(
        """INSERT INTO stories(owner_id,save_state,world_description,category_classification_needed,
                              deleted_at)
           VALUES (100,%s,'기존 세계관',%s,CASE WHEN %s THEN clock_timestamp() END) RETURNING id""",
        (state, pending, deleted),
    ).fetchone()["id"]
    db.execute(
        "INSERT INTO characters(story_id,name,description,position) "
        "VALUES (%s,'내부 이름','상세',0)",
        (story_id,),
    )
    db.execute("INSERT INTO story_categories VALUES (%s,'modern')", (story_id,))
    return story_id


def state(db, story):
    return db.execute(
        """SELECT category_classification_needed, saved_at,
                  ARRAY(SELECT category_code FROM story_categories WHERE story_id=s.id
                        ORDER BY category_code) AS categories
           FROM stories s WHERE id=%s""",
        (story,),
    ).fetchone()


def test_cycle_coalesces_and_filters_CREATE_US_09_AC07(db, db_url):
    story = seed(db)
    excluded = [seed(db, state="draft"), seed(db, pending=False), seed(db, deleted=True)]
    first = seed(db)
    db.execute("DELETE FROM story_categories WHERE story_id=%s", (first,))
    for world in ("첫 수정", "최종 수정"):
        db.execute("UPDATE stories SET world_description=%s WHERE id=%s", (world, story))
    saved_at = state(db, story)["saved_at"]
    calls = []

    def classify(value):
        calls.append((value.world_description, value.character_descriptions))
        return ("romance", "sf_fantasy")

    assert run_cycle(db_url, classify).completed == 1
    assert run_cycle(db_url, classify).selected == 0
    assert calls == [("최종 수정", ("상세",))]
    assert state(db, story) == dict(
        category_classification_needed=False,
        saved_at=saved_at,
        categories=["romance", "sf_fantasy"],
    )
    assert all(state(db, sid)["categories"] == ["modern"] for sid in excluded)
    assert state(db, first)["category_classification_needed"]


def test_cycle_preserves_previous_when_invalid_or_stale_CREATE_US_09_AC07(db, db_url):
    stale, invalid = seed(db), seed(db)
    db.execute("UPDATE stories SET world_description='유효하지 않음' WHERE id=%s", (invalid,))

    def classify(value):
        if value.world_description == "유효하지 않음":
            return ("romance", "unknown")
        with psycopg.connect(db_url, options="-c lock_timeout=2000") as editing:
            editing.execute("UPDATE stories SET world_description='새 버전' WHERE id=%s", (stale,))
        return ("romance",)

    report = run_cycle(db_url, classify)
    assert (report.selected, report.stale, report.failed) == (2, 1, 1)
    for sid in (stale, invalid):
        assert state(db, sid)["category_classification_needed"]
        assert state(db, sid)["categories"] == ["modern"]


def test_only_one_cycle_holds_the_worker_lock_CREATE_US_09_AC07(db, db_url):
    seed(db)

    def classify(_):
        def unexpected(_):
            raise AssertionError("overlapping cycle invoked classifier")

        assert run_cycle(db_url, unexpected).busy
        return ("romance",)

    assert run_cycle(db_url, classify).completed == 1


def test_hourly_scheduler_uses_one_hour_and_stops_CREATE_US_09_AC07(monkeypatch):
    calls = []
    waits = iter((False, False, True))
    now = 0
    monkeypatch.setattr(worker, "monotonic", lambda: now)

    class Stop(Event):
        def wait(self, timeout=None):
            nonlocal now
            assert timeout is not None
            calls.append(timeout)
            now += timeout
            return next(waits)

    def cycle():
        nonlocal now
        calls.append("cycle")
        now += 30
        return CycleReport()

    run_hourly(cycle, Stop())
    assert calls == [3600, "cycle", 3570, "cycle", 3570]
