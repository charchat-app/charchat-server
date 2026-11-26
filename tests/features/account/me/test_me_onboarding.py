"""BASE-03 내 계정(GET/PATCH /me)과 온보딩(pending → active).

필수 동의 3개(현재 버전의 약관·개인정보, 만 14세 이상 self-v1)와 기본 플레이 이름이
모두 있어야 active가 된다. 기본 플레이 이름은 기존 user_play_settings에 저장한다.
"""

import pytest

from app.core.config import settings
from tests.features.account.account_helpers import (
    ME,
    STORIES,
    all_consents,
    bearer,
    dev_login,
    onboard,
)


@pytest.fixture
def pending(client):
    return dev_login(client, "newbie")


def patch_me(client, tokens, body):
    return client.patch(ME, headers=bearer(tokens["access_token"]), json=body)


def test_me_shows_pending_account_and_required_versions_BASE_03(client, pending):
    me = client.get(ME, headers=bearer(pending["access_token"])).json()
    assert me == {
        "user_id": pending["user"]["user_id"],
        "status": "pending",
        "providers": ["dev"],
        "default_play_name": None,
        "consents": {"terms": None, "privacy": None, "age_14_plus": None},
        "required_consents": {
            "terms": "terms-2026-11",
            "privacy": "privacy-2026-11",
            "age_14_plus": "self-v1",
        },
        "onboarding_required": True,
    }


def test_onboarding_with_all_consents_and_name_activates_BASE_03(client, db, pending):
    me = onboard(client, pending["access_token"], "민서")
    assert me["status"] == "active"
    assert me["onboarding_required"] is False
    assert me["default_play_name"] == "민서"
    assert me["consents"] == all_consents()
    user_id = pending["user"]["user_id"]
    row = db.execute("SELECT status, onboarded_at FROM users WHERE id = %s", (user_id,)).fetchone()
    assert row["status"] == "active" and row["onboarded_at"] is not None
    play = db.execute(
        "SELECT default_play_name FROM user_play_settings WHERE owner_id = %s", (user_id,)
    ).fetchone()
    assert play == {"default_play_name": "민서"}
    # 이제 보호된 API를 쓸 수 있다.
    assert client.get(STORIES, headers=bearer(pending["access_token"])).status_code == 200


@pytest.mark.parametrize("missing", ["terms", "privacy", "age_14_plus", "name"])
def test_onboarding_stays_pending_without_every_requirement_BASE_03(client, pending, missing):
    consents = {k: v for k, v in all_consents().items() if k != missing}
    body = {"consents": consents}
    if missing != "name":
        body["default_play_name"] = "민서"
    response = patch_me(client, pending, body)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "pending"
    assert client.get(STORIES, headers=bearer(pending["access_token"])).status_code == 403


def test_onboarding_can_finish_across_several_patches_BASE_03(client, pending):
    assert patch_me(client, pending, {"default_play_name": "민서"}).json()["status"] == "pending"
    assert patch_me(client, pending, {"consents": {"terms": "terms-2026-11"}}).json()["status"] == (
        "pending"
    )
    done = patch_me(
        client, pending, {"consents": {"privacy": "privacy-2026-11", "age_14_plus": "self-v1"}}
    )
    assert done.json()["status"] == "active"


def test_existing_default_play_name_counts_for_onboarding_BASE_03(client, db, pending):
    db.execute(
        "INSERT INTO user_play_settings (owner_id, default_play_name) VALUES (%s, '기존 이름')",
        (pending["user"]["user_id"],),
    )
    me = patch_me(client, pending, {"consents": all_consents()}).json()
    assert me["status"] == "active"
    assert me["default_play_name"] == "기존 이름"


@pytest.mark.parametrize(
    "consents",
    [
        {"terms": "terms-2026-01"},
        {"privacy": "old"},
        {"age_14_plus": "self-v0"},
    ],
)
def test_consent_version_mismatch_is_409_and_records_nothing_BASE_03(client, db, pending, consents):
    body = {"default_play_name": "민서", "consents": {**all_consents(), **consents}}
    response = patch_me(client, pending, body)
    assert response.status_code == 409
    assert response.json()["code"] == "CONSENT_VERSION_MISMATCH"
    assert response.json()["required_consents"] == all_consents()
    assert db.execute("SELECT count(*) AS n FROM user_consents").fetchone()["n"] == 0
    assert db.execute("SELECT count(*) AS n FROM user_play_settings").fetchone()["n"] == 0
    me = client.get(ME, headers=bearer(pending["access_token"])).json()
    assert me["status"] == "pending"


def test_new_terms_version_requires_new_consent_BASE_03(client, pending, monkeypatch):
    """서버 약관 버전이 바뀌면 이전 버전 동의로는 활성화되지 않는다."""
    patch_me(client, pending, {"default_play_name": "민서", "consents": all_consents()})
    monkeypatch.setattr(settings, "auth_terms_version", "terms-2027-01")
    other = dev_login(client, "second")
    old = {**all_consents(), "terms": "terms-2026-11"}
    response = patch_me(client, other, {"default_play_name": "민", "consents": old})
    assert response.status_code == 409
    ok = patch_me(client, other, {"default_play_name": "민", "consents": all_consents()})
    assert ok.json()["status"] == "active"
    assert ok.json()["consents"]["terms"] == "terms-2027-01"


@pytest.mark.parametrize(
    "name", ["", "   ", " 민서", "민서 ", "{{user}}", "가" * 21, "a\x00b", "민\n서", "민\r서"]
)
def test_invalid_default_play_name_is_422_BASE_03(client, db, pending, name):
    response = patch_me(client, pending, {"default_play_name": name, "consents": all_consents()})
    assert response.status_code == 422
    assert response.json()["code"] == "PLAY_NAME_INVALID"
    assert db.execute("SELECT count(*) AS n FROM user_consents").fetchone()["n"] == 0


def test_default_play_name_length_limit_is_20_BASE_03(client, db, pending):
    """임시 규칙: 기본 플레이 이름 1~20자. 21자는 할 일을 알려 주는 detail로 거절, 저장 안 함."""
    for name in ("가" * 21, "a" * 21, "가" * 1000):
        response = patch_me(client, pending, {"default_play_name": name})
        assert response.status_code == 422
        assert response.json()["code"] == "PLAY_NAME_INVALID"
        assert response.json()["detail"] == "플레이 이름은 20자 이하로 입력하세요."
    assert db.execute("SELECT count(*) AS n FROM user_play_settings").fetchone()["n"] == 0
    for name in ("가" * 20, "a", "민 서"):
        response = patch_me(client, pending, {"default_play_name": name})
        assert response.status_code == 200, response.text
        assert response.json()["default_play_name"] == name


@pytest.mark.parametrize(
    "body",
    [
        {"default_play_name": 3},
        {"status": "active"},
        {"consents": {"marketing": "v1"}},
        {"consents": {"terms": ""}},
    ],
)
def test_me_update_validation_is_422_BASE_03(client, pending, body):
    response = patch_me(client, pending, body)
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_active_account_can_rename_and_stays_active_BASE_03(client, pending):
    onboard(client, pending["access_token"], "민서")
    me = patch_me(client, pending, {"default_play_name": "지우"}).json()
    assert me["status"] == "active"
    assert me["default_play_name"] == "지우"


def test_me_requires_authentication_BASE_03(client):
    assert client.get(ME).status_code == 401
    assert client.patch(ME, json={}).status_code == 401


def test_me_with_dev_header_for_missing_account_is_404_BASE_03(client, dev_user_header_enabled):
    response = client.get(ME, headers={"X-User-Id": "424242"})
    assert response.status_code == 404
    assert response.json()["code"] == "ACCOUNT_NOT_FOUND"
