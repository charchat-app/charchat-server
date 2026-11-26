"""BASE-03 공개 이미지 조회의 선택적 사용자(get_optional_user_id)도 공통 인증 규칙을 따른다."""

import pytest

from app.core.config import settings
from app.features.creation.images.storage import LocalImageStorage, get_image_storage
from tests.features.account.account_helpers import active_login, bearer, dev_login
from tests.features.creation.images.helpers import PNG, upload


@pytest.fixture
def image_api(client, tmp_path):
    storage = LocalImageStorage(tmp_path / "media")
    client.app.dependency_overrides[get_image_storage] = lambda: storage
    yield client
    client.app.dependency_overrides.clear()


@pytest.fixture
def private_image(image_api):
    """아직 어디에도 연결하지 않은 업로드: 소유자만 볼 수 있다."""
    owner = active_login(image_api, "owner")
    response = upload(image_api, PNG, "story", headers=bearer(owner["access_token"]))
    assert response.status_code == 201, response.text
    return owner, response.json()["url"]


def test_owner_bearer_sees_private_image_others_do_not_BASE_03(image_api, private_image):
    owner, url = private_image
    other = active_login(image_api, "other")
    assert image_api.get(url, headers=bearer(owner["access_token"])).status_code == 200
    assert image_api.get(url, headers=bearer(other["access_token"])).status_code == 404
    assert image_api.get(url).status_code == 404


def test_dev_header_is_ignored_for_image_viewer_when_flag_off_BASE_03(
    image_api, private_image, monkeypatch
):
    owner, url = private_image
    owner_header = {"X-User-Id": str(owner["user"]["user_id"])}
    monkeypatch.setattr(settings, "auth_dev_header_enabled", True)
    assert image_api.get(url, headers=owner_header).status_code == 200
    monkeypatch.setattr(settings, "auth_dev_header_enabled", False)
    # 꺼져 있으면 헤더를 보지 않아 익명 조회와 같다.
    assert image_api.get(url, headers=owner_header).status_code == 404


def test_image_viewer_bearer_wins_and_bad_or_pending_token_is_rejected_BASE_03(
    image_api, private_image
):
    owner, url = private_image
    other = active_login(image_api, "other")
    mixed = {**bearer(other["access_token"]), "X-User-Id": str(owner["user"]["user_id"])}
    assert image_api.get(url, headers=mixed).status_code == 404
    bad = image_api.get(url, headers=bearer("cca_" + "A" * 43))
    assert bad.status_code == 401
    assert bad.json()["code"] == "AUTH_TOKEN_INVALID"
    pending = dev_login(image_api, "pending-viewer")
    response = image_api.get(url, headers=bearer(pending["access_token"]))
    assert response.status_code == 403
    assert response.json()["code"] == "ONBOARDING_REQUIRED"
