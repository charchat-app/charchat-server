"""CREATE-US-06 이미지 업로드·목록 입출력."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# AC02: 모든 대상에 파일당 최대 5MB.
MAX_IMAGE_BYTES = 5 * 1024 * 1024
# AC03: 스토리 최대 10장, 등장 캐릭터 1~5장(완성 저장 기준), 유저 프로필 1장.
MAX_STORY_IMAGES = 10
MAX_CHARACTER_IMAGES = 5

ImageTarget = Literal["story", "character", "profile"]
MimeType = Literal["image/jpeg", "image/png", "image/webp", "image/gif"]


class ImageItem(BaseModel):
    image_id: int
    mime_type: MimeType
    byte_size: int
    url: str


class CharacterImages(BaseModel):
    character_id: int
    name: str | None
    images: list[ImageItem]


class CoverImage(BaseModel):
    """AC09: 스토리 이미지가 없으면 첫 번째 캐릭터의 첫 이미지를 대체 표지로 쓴다."""

    source: Literal["story", "character"]
    character_id: int | None = None
    image: ImageItem


class StoryImagesResponse(BaseModel):
    story_id: int
    save_state: Literal["draft", "private", "public"]
    saved_at: datetime
    story_images: list[ImageItem]
    characters: list[CharacterImages]
    cover: CoverImage | None
    # AC08: 필수 이미지가 없는 등장 캐릭터. 임시 저장은 허용하고 완성 저장에서만 막는다.
    characters_missing_images: list[int]
    request_id: str | None = None


def _no_duplicates(ids: list[int], label: str) -> None:
    if len(set(ids)) != len(ids):
        raise ValueError(f"{label} 목록에 같은 이미지를 두 번 넣을 수 없습니다.")


class CharacterImagesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    character_id: int = Field(gt=0, le=9223372036854775807)
    image_ids: list[int] = Field(max_length=MAX_CHARACTER_IMAGES)

    @model_validator(mode="after")
    def check_ids(self) -> "CharacterImagesInput":
        _no_duplicates(self.image_ids, "캐릭터 이미지")
        if any(i <= 0 for i in self.image_ids):
            raise ValueError("이미지 ID는 양의 정수여야 합니다.")
        return self


class StoryImagesSaveRequest(BaseModel):
    """목록 전체를 보낸다. 순서가 곧 position이며 첫 이미지가 대표다(AC06).

    `characters`에 넣은 캐릭터만 바꾸고, 빠진 캐릭터의 이미지는 그대로 둔다.
    교체(AC07)는 같은 자리에 새 image_id를 넣어 보낸다.
    """

    model_config = ConfigDict(extra="forbid")

    story_image_ids: list[int] = Field(max_length=MAX_STORY_IMAGES)
    characters: list[CharacterImagesInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_ids(self) -> "StoryImagesSaveRequest":
        _no_duplicates(self.story_image_ids, "스토리 이미지")
        if any(i <= 0 for i in self.story_image_ids):
            raise ValueError("이미지 ID는 양의 정수여야 합니다.")
        character_ids = [c.character_id for c in self.characters]
        if len(set(character_ids)) != len(character_ids):
            raise ValueError("같은 캐릭터를 두 번 보낼 수 없습니다.")
        return self
