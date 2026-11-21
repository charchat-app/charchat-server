"""CREATE-US-05 프리셋 검증·읽기·교체 단위 테스트 (DB 없음).

저장소(repository)는 메모리 가짜로 바꿔 끼운다. 실제 DB·연결·픽스처를 쓰지 않는다.
TODO: 인수 조건 번호(CREATE-US-05-ACxx)는 kickoff 문서를 읽은 뒤
테스트 이름에 반영한다. 지금은 스토리 ID(CREATE_US_05)만 적었다.
"""

from types import SimpleNamespace

import pytest
from psycopg.pq import TransactionStatus
from pydantic import ValidationError

from app.features.creation.user_profile import service
from app.features.creation.user_profile.schemas import AuthorizedStory, PresetDraft

STORY = AuthorizedStory(story_id=1, owner_id=10)


def D(**kw) -> PresetDraft:
    return PresetDraft(**kw)


def _codes(issues) -> list[tuple[str, str]]:
    return [(i.field, i.code) for i in issues]


# ---------------------------------------------------------------- 가짜 저장소


class _NoTouchConn:
    """어떤 속성 접근도 실패한다. DB를 건드리면 안 되는 경로를 검증한다."""

    def __getattr__(self, name):
        raise AssertionError(f"DB 연결을 건드렸다: {name}")


def _conn(status=TransactionStatus.INTRANS):
    # commit/rollback/transaction 속성이 없으므로 서비스가 호출하면 실패한다.
    return SimpleNamespace(info=SimpleNamespace(transaction_status=status))


class FakeStore:
    def __init__(self, rows=(), images=None, story_visible=True):
        self.rows = [dict(r) for r in rows]
        self.images = images if images is not None else {}
        self.story_visible = story_visible
        self.next_id = 100
        self.writes: list[str] = []
        self.image_lookups: list[list[int]] = []

    def snapshot(self):
        return [dict(r) for r in self.rows]

    def install(self, monkeypatch):
        repo = service.repo
        monkeypatch.setattr(repo, "lock_story", self.lock_story)
        monkeypatch.setattr(repo, "list_presets", self.list_presets)
        monkeypatch.setattr(repo, "find_image_owners", self.find_image_owners)
        monkeypatch.setattr(repo, "delete_presets_except", self.delete_presets_except)
        monkeypatch.setattr(repo, "update_preset", self.update_preset)
        monkeypatch.setattr(repo, "insert_preset", self.insert_preset)
        # 미사용 표시 재계산(images 서비스 공개 함수)은 DB 통합검사(test_presets_db.py)가 검증한다.
        monkeypatch.setattr(
            service.images_service, "refresh_orphaned_images", self.refresh_orphaned
        )
        self.refreshed: list[set[int]] = []

    def refresh_orphaned(self, conn, image_ids):
        self.refreshed.append(set(image_ids))

    def lock_story(self, conn, story_id, owner_id):
        return self.story_visible and (story_id, owner_id) == (STORY.story_id, STORY.owner_id)

    def list_presets(self, conn, story_id):
        rows = [r for r in self.rows if r["story_id"] == story_id]
        rows.sort(key=lambda r: (r["position"], r["id"]))
        return [{k: v for k, v in r.items() if k != "story_id"} for r in rows]

    def find_image_owners(self, conn, image_ids):
        self.image_lookups.append(list(image_ids))
        return {i: self.images[i] for i in image_ids if i in self.images}

    def delete_presets_except(self, conn, story_id, keep_ids):
        self.writes.append("delete")
        self.rows = [r for r in self.rows if r["story_id"] != story_id or r["id"] in keep_ids]

    def update_preset(self, conn, story_id, preset_id, name, image_id, description, position):
        self.writes.append("update")
        for r in self.rows:
            if r["id"] == preset_id and r["story_id"] == story_id:
                r.update(name=name, image_id=image_id, description=description, position=position)
                return True
        return False

    def insert_preset(self, conn, story_id, name, image_id, description, position):
        self.writes.append("insert")
        self.next_id += 1
        self.rows.append(
            dict(
                id=self.next_id,
                story_id=story_id,
                name=name,
                image_id=image_id,
                description=description,
                position=position,
            )
        )


def _row(id, position, name="n", image_id=500, description="d", story_id=1):
    return dict(
        id=id,
        story_id=story_id,
        name=name,
        image_id=image_id,
        description=description,
        position=position,
    )


@pytest.fixture
def store(monkeypatch):
    s = FakeStore(images={500: 10, 501: 10, 900: 99})
    s.install(monkeypatch)
    return s


# ---------------------------------------------------------------- 입력 모델


def test_preset_draft_keeps_raw_text_CREATE_US_05():
    d = D(name="  {{user}}  ", description="\n긴 글 {{user}} \n")
    assert d.name == "  {{user}}  "
    assert d.description == "\n긴 글 {{user}} \n"


@pytest.mark.parametrize("bad", [0, -1, "5", True, 1.5])
def test_preset_draft_rejects_non_positive_or_loose_ids_CREATE_US_05(bad):
    with pytest.raises(ValidationError):
        D(id=bad)
    with pytest.raises(ValidationError):
        D(image_id=bad)


def test_preset_draft_allows_all_fields_empty_for_draft_CREATE_US_05():
    d = D()
    assert (d.id, d.name, d.image_id, d.description) == (None, None, None, None)


# DB 열 한도: id·image_id는 PostgreSQL bigint, 텍스트는 text(NUL 문자를 저장할 수 없음).
# 스키마에서 거르지 않으면 저장 단계에서 DB 오류(500)로 터진다.
BIGINT_MAX = 2**63 - 1


@pytest.mark.parametrize("field", ["id", "image_id"])
def test_preset_draft_accepts_ids_up_to_bigint_max_CREATE_US_05(field):
    assert getattr(D(**{field: BIGINT_MAX}), field) == BIGINT_MAX


@pytest.mark.parametrize("field", ["id", "image_id"])
@pytest.mark.parametrize("value", [BIGINT_MAX + 1, 2**64, 10**30])
def test_preset_draft_rejects_ids_above_bigint_CREATE_US_05(field, value):
    with pytest.raises(ValidationError):
        D(**{field: value})


@pytest.mark.parametrize("field", ["name", "description"])
@pytest.mark.parametrize("text", ["\x00", "민\x00수", "{{user}}\x00", "\x00 앞뒤 공백 "])
def test_preset_draft_rejects_nul_character_in_text_CREATE_US_05(field, text):
    with pytest.raises(ValidationError):
        D(**{field: text})


@pytest.mark.parametrize("field", ["name", "description"])
def test_preset_draft_still_keeps_other_control_whitespace_raw_CREATE_US_05(field):
    # NUL만 거절한다. 줄바꿈·탭 같은 원문은 그대로 보존한다(자르거나 바꾸지 않음).
    text = "\t가\n나\r\n"
    assert getattr(D(**{field: text}), field) == text


# ---------------------------------------------------------------- 완성 조건 검사


def test_zero_presets_is_not_an_issue_CREATE_US_05():
    assert service.collect_preset_issues([], []) == []


def test_up_to_five_presets_ok_and_six_exceeds_CREATE_US_05():
    five = [D(name=f"이름{i}", image_id=1) for i in range(5)]
    assert service.collect_preset_issues(five, []) == []
    six = [*five, D(name="여섯째", image_id=1)]
    issues = service.collect_preset_issues(six, [])
    assert _codes(issues) == [("presets", "PRESET_COUNT_EXCEEDED")]


def test_name_and_image_required_for_completion_but_description_optional_CREATE_US_05():
    drafts = [D(), D(name="  ", image_id=1), D(name="가", image_id=1)]
    issues = service.collect_preset_issues(drafts, [])
    assert _codes(issues) == [
        ("presets[0].name", "REQUIRED"),
        ("presets[0].image_id", "REQUIRED"),
        ("presets[1].name", "REQUIRED"),
    ]


def test_surrounding_space_in_direct_name_is_format_issue_CREATE_US_05():
    issues = service.collect_preset_issues([D(name=" 민수", image_id=1)], [])
    assert _codes(issues) == [("presets[0].name", "NAME_SURROUNDING_SPACE")]


def test_user_placeholder_is_valid_name_and_repeatable_CREATE_US_05():
    drafts = [D(name="{{user}}", image_id=1), D(name="{{user}}", image_id=2)]
    assert service.collect_preset_issues(drafts, ["user", "{{user}}"]) == []


def test_placeholder_is_case_sensitive_and_exact_CREATE_US_05():
    # 정확히 `{{user}}`만 자리표시자다. 그 외는 직접 이름으로 검사한다.
    assert service.collect_preset_issues([D(name="{{USER}}", image_id=1)], []) == []
    dup = [D(name="{{USER}}", image_id=1), D(name="{{ user }}", image_id=2)]
    assert _codes(service.collect_preset_issues(dup, [])) == [
        ("presets[0].name", "NAME_DUPLICATE_PRESET"),
        ("presets[1].name", "NAME_DUPLICATE_PRESET"),
    ]


@pytest.mark.parametrize("char_name", ["Alex", "alex", " A l e x"])
def test_direct_name_duplicates_character_ignoring_case_and_spaces_CREATE_US_05(char_name):
    issues = service.collect_preset_issues([D(name="a LEX", image_id=1)], [char_name])
    assert _codes(issues) == [("presets[0].name", "NAME_DUPLICATE_CHARACTER")]


def test_direct_names_duplicate_each_other_but_placeholder_does_not_CREATE_US_05():
    drafts = [
        D(name="이도형", image_id=1),
        D(name="이 도형", image_id=1),
        D(name="{{user}}", image_id=1),
        D(name="다른", image_id=1),
    ]
    assert _codes(service.collect_preset_issues(drafts, [])) == [
        ("presets[0].name", "NAME_DUPLICATE_PRESET"),
        ("presets[1].name", "NAME_DUPLICATE_PRESET"),
    ]


def test_blank_or_missing_character_names_do_not_match_CREATE_US_05():
    issues = service.collect_preset_issues([D(name="민수", image_id=1)], [None, "", "  "])
    assert issues == []


def test_text_limit_counts_raw_name_plus_description_per_preset_CREATE_US_05():
    ok = D(name="{{user}}", image_id=1, description="가" * (1000 - len("{{user}}")))
    over = D(name="{{user}}", image_id=1, description="가" * (1000 - len("{{user}}") + 1))
    assert service.collect_preset_issues([ok], []) == []
    issues = service.collect_preset_issues([ok, over], [])
    assert _codes(issues) == [("presets[1].text", "PRESET_TEXT_TOO_LONG")]
    assert issues[0].value == "1001"


def test_text_limit_counts_surrounding_whitespace_CREATE_US_05():
    d = D(name="가", image_id=1, description=" " * 1000)
    issues = service.collect_preset_issues([d], [])
    assert _codes(issues) == [("presets[0].text", "PRESET_TEXT_TOO_LONG")]


# ---------------------------------------------------------------- 읽기


def test_read_presets_returns_saved_order_including_drafts(store):
    store.rows = [_row(2, 1, name=None, image_id=None), _row(1, 0), _row(9, 0, story_id=2)]
    result = service.read_presets(_conn(), STORY)
    assert [(p.id, p.position, p.name, p.image_id) for p in result] == [
        (1, 0, "n", 500),
        (2, 1, None, None),
    ]


# ---------------------------------------------------------------- 교체: 생략·비움·트랜잭션


def test_omitted_presets_do_not_touch_db_CREATE_US_05():
    assert service.replace_presets(_NoTouchConn(), STORY, None, character_names=[]) is None


def test_explicit_empty_list_deletes_all_presets_CREATE_US_05(store):
    store.rows = [_row(1, 0), _row(2, 1), _row(9, 0, story_id=2)]
    result = service.replace_presets(_conn(), STORY, [], character_names=[])
    assert result.presets == []
    assert result.issues == []
    assert [r["id"] for r in store.rows] == [9]  # 다른 스토리 프리셋은 그대로


def test_requires_caller_held_transaction(store):
    store.rows = [_row(1, 0)]
    for status in (TransactionStatus.IDLE, TransactionStatus.INERROR):
        with pytest.raises(RuntimeError):
            service.replace_presets(_conn(status), STORY, [], character_names=[])
    assert store.writes == []
    assert len(store.rows) == 1


def test_story_not_visible_to_owner_is_rejected_without_writes(store):
    store.story_visible = False
    store.rows = [_row(1, 0)]
    with pytest.raises(service.PresetStoryNotFoundError):
        service.replace_presets(_conn(), STORY, [], character_names=[])
    assert store.writes == []


# ---------------------------------------------------------------- 교체: id 보존·순서


def test_edit_and_reorder_preserve_ids_CREATE_US_05(store):
    store.rows = [_row(1, 0, name="가"), _row(2, 1, name="나"), _row(3, 2, name="다")]
    drafts = [
        D(id=3, name="다2", image_id=500),
        D(id=1, name="가", image_id=500),
        D(id=2, name="나", image_id=501),
    ]
    result = service.replace_presets(_conn(), STORY, drafts, character_names=[])
    assert [(p.id, p.position, p.name) for p in result.presets] == [
        (3, 0, "다2"),
        (1, 1, "가"),
        (2, 2, "나"),
    ]
    assert "insert" not in store.writes


def test_removed_deleted_and_new_gets_new_id_CREATE_US_05(store):
    store.rows = [_row(1, 0), _row(2, 1)]
    drafts = [D(id=2, name="나", image_id=500), D(name="새", image_id=501)]
    result = service.replace_presets(_conn(), STORY, drafts, character_names=[])
    assert [(p.id, p.position, p.name) for p in result.presets] == [(2, 0, "나"), (101, 1, "새")]


def test_incomplete_drafts_are_saved_raw_and_reported_CREATE_US_05(store):
    long_text = "가" * 5000
    drafts = [
        D(name=" {{user}} ", image_id=None, description=long_text),
        D(name=None, image_id=500, description=None),
    ]
    result = service.replace_presets(_conn(), STORY, drafts, character_names=[])
    saved = {p.position: p for p in result.presets}
    assert saved[0].name == " {{user}} "
    assert saved[0].description == long_text
    assert saved[0].image_id is None
    assert saved[1].name is None
    assert set(_codes(result.issues)) == {
        ("presets[0].name", "NAME_SURROUNDING_SPACE"),
        ("presets[0].image_id", "REQUIRED"),
        ("presets[0].text", "PRESET_TEXT_TOO_LONG"),
        ("presets[1].name", "REQUIRED"),
    }


def test_issues_use_candidate_character_names_CREATE_US_05(store):
    drafts = [D(name="Alex", image_id=500)]
    result = service.replace_presets(_conn(), STORY, drafts, character_names=["alex"])
    assert _codes(result.issues) == [("presets[0].name", "NAME_DUPLICATE_CHARACTER")]
    assert [p.name for p in result.presets] == ["Alex"]  # 초안은 그대로 저장


def test_more_than_five_drafts_are_kept_and_reported(store):
    drafts = [D(name=f"이름{i}", image_id=500) for i in range(6)]
    result = service.replace_presets(_conn(), STORY, drafts, character_names=[])
    assert len(result.presets) == 6
    assert _codes(result.issues) == [("presets", "PRESET_COUNT_EXCEEDED")]


# ---------------------------------------------------------------- 교체: 구조 오류


def test_other_story_or_unknown_preset_id_rejected_before_any_write_CREATE_US_05(store):
    store.rows = [_row(1, 0), _row(9, 0, story_id=2)]
    before = store.snapshot()
    with pytest.raises(service.InvalidPresetReferenceError) as exc:
        service.replace_presets(
            _conn(), STORY, [D(id=1, name="a"), D(id=9, name="b")], character_names=[]
        )
    assert exc.value.extra == {"preset_ids": [9]}
    assert store.writes == []
    assert store.snapshot() == before


def test_duplicate_preset_id_rejected_before_any_write(store):
    store.rows = [_row(1, 0)]
    with pytest.raises(service.InvalidPresetReferenceError):
        service.replace_presets(
            _conn(), STORY, [D(id=1, name="a"), D(id=1, name="b")], character_names=[]
        )
    assert store.writes == []


@pytest.mark.parametrize("image_id", [900, 777])  # 남의 이미지, 존재하지 않는 이미지
def test_unavailable_new_image_rejected_with_same_error_CREATE_US_05(store, image_id):
    store.rows = [_row(1, 0)]
    before = store.snapshot()
    with pytest.raises(service.InvalidPresetImageError) as exc:
        service.replace_presets(
            _conn(), STORY, [D(id=1, name="a", image_id=image_id)], character_names=[]
        )
    assert exc.value.extra == {"image_ids": [image_id]}
    assert store.writes == []
    assert store.snapshot() == before


def test_unchanged_existing_image_is_locked_but_not_rejected_CREATE_US_05(store):
    # 이미 연결된 이미지는 소유자가 달라 보여도 다른 필드 수정을 막지 않는다.
    store.rows = [_row(1, 0, image_id=900)]
    result = service.replace_presets(
        _conn(), STORY, [D(id=1, name="수정", image_id=900)], character_names=[]
    )
    assert result.presets[0].name == "수정"
    assert store.image_lookups == [[900]]


def test_old_and_new_image_ids_are_locked_in_a_single_lookup(store):
    store.rows = [_row(1, 0, image_id=500)]
    drafts = [
        D(id=1, name="a", image_id=500),
        D(name="b", image_id=501),
        D(name="c", image_id=501),
    ]
    service.replace_presets(_conn(), STORY, drafts, character_names=[])
    assert store.image_lookups == [[500, 501]]


def test_missing_image_is_a_completion_issue_not_a_structural_error_CREATE_US_05(store):
    result = service.replace_presets(_conn(), STORY, [D(name="a")], character_names=[])
    assert _codes(result.issues) == [("presets[0].image_id", "REQUIRED")]
    assert store.image_lookups == []
