"""CREATE-US-06 테스트 픽스처. 공통 `db_url`·`db`·`client`(tests/conftest.py)를 쓴다.

이미지 파일은 테스트마다 새 임시 폴더에 저장한다. 실제 MEDIA_ROOT는 건드리지 않는다.
"""

import pytest

from app.features.creation.images.storage import LocalImageStorage, get_image_storage
from tests.features.creation.images.helpers import OWNER_ID


@pytest.fixture
def storage(tmp_path):
    return LocalImageStorage(tmp_path / "media")


@pytest.fixture
def api(client, storage):
    client.headers["X-User-Id"] = str(OWNER_ID)
    client.app.dependency_overrides[get_image_storage] = lambda: storage
    yield client
    client.app.dependency_overrides.clear()
