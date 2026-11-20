"""CREATE-US-06 이미지 파일 저장소.

호스팅이 정해지기 전까지 각자 로컬 디스크(`MEDIA_ROOT`)에 저장한다.
DB에는 저장소 안의 상대 키(`storage_key`)만 남기므로, 나중에 S3 등으로 바꿀 때는
이 모듈의 구현만 교체한다.
"""

import re
import secrets
from pathlib import Path
from typing import Protocol

from app.core.config import settings

# 저장 키 형식. 경로 이동(../)을 막기 위해 무작위 32자리 16진수와 확장자만 허용한다.
STORAGE_KEY_PATTERN = re.compile(r"^[0-9a-f]{32}\.(jpg|png|webp|gif)$")


class ImageStorage(Protocol):
    def save(self, data: bytes, extension: str) -> str:
        """파일을 저장하고 저장 키를 돌려준다."""

    def path_of(self, storage_key: str) -> Path | None:
        """저장 키에 해당하는 실제 파일 경로. 형식이 틀리거나 파일이 없으면 None."""

    def delete(self, storage_key: str) -> None:
        """파일을 지운다. 이미 없으면 성공으로 본다."""


class LocalImageStorage:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def save(self, data: bytes, extension: str) -> str:
        self.root.mkdir(parents=True, exist_ok=True)
        key = f"{secrets.token_hex(16)}.{extension}"
        target = self.root / key
        temp = target.with_suffix(target.suffix + ".part")
        # 쓰다가 실패한 파일이 정상 파일로 보이지 않게 임시 이름으로 쓴 뒤 바꾼다.
        temp.write_bytes(data)
        temp.replace(target)
        return key

    def path_of(self, storage_key: str) -> Path | None:
        if not STORAGE_KEY_PATTERN.fullmatch(storage_key):
            return None
        path = self.root / storage_key
        return path if path.is_file() else None

    def delete(self, storage_key: str) -> None:
        if not STORAGE_KEY_PATTERN.fullmatch(storage_key):
            return
        (self.root / storage_key).unlink(missing_ok=True)


def get_image_storage() -> ImageStorage:
    """FastAPI 의존성. 테스트는 dependency_overrides로 임시 폴더 저장소를 넣는다."""
    return LocalImageStorage(settings.media_root)
