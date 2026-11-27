"""CREATE-US-08-AC15 공통 정본 읽기(`read_story_surface`)의 범위(DB 필요, 외부 호출 없음).

- 관계는 null이 아닌 양 끝이 모두 같은 스토리일 때만 읽는다(다른 스토리 이름·관계명 금지).
- 극 중 유저 자리(null 끝)는 `{{user}}`로 보존한다.
- 제작자 프리셋은 정본으로 항상 읽고, 플레이어 개인 프로필은 읽지 않는다.
"""

from app.core.common_safety import (
    USER_SLOT,
    SafetyProfile,
    read_story_surface,
    story_is_clear,
)
from tests.common_safety_fakes import FakeGuard
from tests.features.conversation.starter.test_starter_common_safety import seed

OTHER_NAME = "ZQX-OTHERNAME-4401"
OTHER_LABEL = "ZQX-OTHERLABEL-4402"
PERSONAL = "ZQX-PERSONAL-4403"


def _other_story(db) -> int:
    story = db.execute(
        """INSERT INTO stories (owner_id, title, subtitle, world_description, save_state)
           VALUES (3, 'Other', 'Sub', 'World', 'public') RETURNING id"""
    ).fetchone()["id"]
    return db.execute(
        """INSERT INTO characters (story_id, name, description, position)
           VALUES (%s, %s, 'Other desc', 0) RETURNING id""",
        (story, OTHER_NAME),
    ).fetchone()["id"]


def _character(db, story_id, name):
    return db.execute(
        "SELECT id FROM characters WHERE story_id = %s AND name = %s", (story_id, name)
    ).fetchone()["id"]


def _relate(db, source, target, label=OTHER_LABEL):
    db.execute(
        """INSERT INTO character_initial_relationships
               (source_kind, source_character_id, target_kind, target_character_id, label)
           VALUES ('character', %s, 'character', %s, %s)""",
        (source, target, label),
    )


def test_cross_story_relationship_is_never_read_CREATE_US_08_AC15(db):
    story = seed(db, "public")
    aria = _character(db, story["id"], "Aria")
    foreign = _other_story(db)
    _relate(db, aria, foreign)  # 이 스토리 → 다른 스토리
    _relate(db, foreign, aria)  # 다른 스토리 → 이 스토리

    surface = read_story_surface(db, story["id"])

    assert {(r.source, r.target, r.label) for r in surface.relationships} == {
        ("Aria", "Bea", "Rival label"),
        (USER_SLOT, "Bea", "Ally label"),  # 진짜 유저 자리는 보존한다.
    }
    assert OTHER_NAME not in repr(surface)
    assert OTHER_LABEL not in repr(surface)


def test_user_slot_relationship_to_other_story_is_not_read_CREATE_US_08_AC15(db):
    story = seed(db, "public")
    foreign = _other_story(db)
    db.execute(
        """INSERT INTO character_initial_relationships
               (source_kind, source_character_id, target_kind, target_character_id, label)
           VALUES ('user', NULL, 'character', %s, %s)""",
        (foreign, OTHER_LABEL),
    )
    surface = read_story_surface(db, story["id"])
    assert OTHER_LABEL not in repr(surface)
    assert (USER_SLOT, "Bea", "Ally label") in {
        (r.source, r.target, r.label) for r in surface.relationships
    }


def test_canonical_creator_presets_are_read_without_personal_profiles_CREATE_US_08_AC15(db):
    story = seed(db, "public")
    image = db.execute("SELECT id FROM images LIMIT 1").fetchone()["id"]
    # 이미지 없는 초안 프리셋도 제작자가 쓴 정본이므로 판정 범위에 넣는다.
    db.execute(
        """INSERT INTO story_user_presets (story_id, name, image_id, description, position)
           VALUES (%s, 'Draft', NULL, NULL, 1)""",
        (story["id"],),
    )
    db.execute(
        "INSERT INTO user_profiles (owner_id, name, image_id, description) VALUES (2, %s, %s, %s)",
        (PERSONAL, image, PERSONAL),
    )
    surface = read_story_surface(db, story["id"])
    assert surface.bound_profiles == (
        SafetyProfile(name=USER_SLOT, description="Preset"),
        SafetyProfile(name="Draft", description=""),
    )
    assert PERSONAL not in repr(surface)
    assert surface.session_origin is False


def test_session_origin_marker_is_carried_CREATE_US_08_AC15(db):
    story = seed(db, "public")
    guard = FakeGuard()
    assert read_story_surface(db, story["id"], session_origin=True).session_origin is True
    assert story_is_clear(db, guard, story["id"], session_origin=True)
    assert [s.session_origin for s in guard.surfaces] == [True]
