"""CREATE-US-06 AC02·AC05 업로드 검사와 파일 조회의 실제 PostgreSQL 통합검사."""

import pytest

from app.features.creation.images import repository
from app.features.creation.images.schemas import MAX_IMAGE_BYTES
from tests.features.creation.images.helpers import GIF, JPEG, PNG, WEBP, upload


def files_in(storage):
    return sorted(p.name for p in storage.root.glob("*")) if storage.root.exists() else []


def test_upload_requires_user_header(client):
    response = client.post(
        "/api/v1/creation/images",
        params={"target": "story"},
        files={"file": ("a.png", PNG, "image/png")},
    )
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHENTICATED"


@pytest.mark.parametrize(
    ("data", "mime_type"),
    [(PNG, "image/png"), (JPEG, "image/jpeg"), (WEBP, "image/webp")],
)
def test_still_image_upload_creates_unlinked_image_and_file_CREATE_US_06_AC02(
    api, db, storage, data, mime_type
):
    response = upload(api, data, target="character")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["mime_type"] == mime_type
    assert body["byte_size"] == len(data)

    row = db.execute("SELECT * FROM images WHERE id=%s", (body["image_id"],)).fetchone()
    assert row["owner_id"] == 100
    # 업로드 직후에는 아무 대상에도 연결되지 않은 미사용 상태다.
    assert row["orphaned_at"] is not None
    assert files_in(storage) == [row["storage_key"]]

    served = api.get(body["url"])
    assert served.status_code == 200
    assert served.headers["content-type"] == mime_type
    assert served.content == data


def test_format_is_judged_by_content_not_name_or_content_type_CREATE_US_06_AC02(api, db, storage):
    # 이름과 Content-Type이 PNG여도 내용이 이미지가 아니면 거절한다.
    fake = api.post(
        "/api/v1/creation/images",
        params={"target": "story"},
        files={"file": ("photo.png", b"not an image at all", "image/png")},
    )
    bmp = upload(api, b"BM" + b"\x00" * 40, target="story", name="a.bmp")
    for response in (fake, bmp):
        assert response.status_code == 422
        assert response.json()["code"] == "IMAGE_UNSUPPORTED_FORMAT"
    empty = upload(api, b"", target="story")
    assert empty.json()["code"] == "IMAGE_EMPTY"
    assert db.execute("SELECT count(*) AS n FROM images").fetchone()["n"] == 0
    assert files_in(storage) == []


def test_five_megabytes_is_the_per_file_limit_CREATE_US_06_AC02(api, db, storage):
    exact = PNG + b"\x00" * (MAX_IMAGE_BYTES - len(PNG))
    assert upload(api, exact).status_code == 201

    over = upload(api, exact + b"\x00")
    assert over.status_code == 422
    assert over.json()["code"] == "IMAGE_TOO_LARGE"
    assert over.json()["limit_bytes"] == MAX_IMAGE_BYTES
    assert db.execute("SELECT count(*) AS n FROM images").fetchone()["n"] == 1
    assert len(files_in(storage)) == 1


def test_gif_only_for_story_and_kept_byte_for_byte_CREATE_US_06_AC05(api, db, storage):
    for target in ("character", "profile"):
        response = upload(api, GIF, target=target, name="a.gif")
        assert response.status_code == 422
        assert response.json()["code"] == "IMAGE_GIF_NOT_ALLOWED"
    assert files_in(storage) == []

    story_gif = upload(api, GIF, target="story", name="a.gif")
    assert story_gif.status_code == 201
    assert story_gif.json()["mime_type"] == "image/gif"
    # 원본을 자르거나 재인코딩하지 않고 그대로 돌려준다.
    assert api.get(story_gif.json()["url"]).content == GIF


def test_file_is_removed_when_image_row_cannot_be_saved(api, storage, monkeypatch):
    def broken_insert(*args, **kwargs):
        raise RuntimeError("DB 기록 실패")

    monkeypatch.setattr(repository, "insert_image", broken_insert)
    with pytest.raises(RuntimeError):
        upload(api, PNG)
    assert files_in(storage) == []


@pytest.mark.parametrize(
    "key",
    ["0" * 32 + ".png", "..%2F..%2Fetc%2Fpasswd", "abc.png", "0" * 32 + ".exe"],
)
def test_unknown_or_malformed_file_keys_are_not_found(api, key):
    response = api.get(f"/api/v1/creation/image-files/{key}")
    assert response.status_code == 404
