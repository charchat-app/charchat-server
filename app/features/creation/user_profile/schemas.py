"""CREATE-US-05 유저 프로필 프리셋 입출력 모델.

- 입력(`PresetDraft`)은 원문 그대로 담는다.
  앞뒤 공백·긴 글·`{{user}}`를 자르거나 바꾸지 않는다.
- 구조 오류(id·이미지 참조가 잘못됨)와 완성 저장 조건 위반(`PresetIssue`)은 분리한다.
  구조 오류는 쓰기 자체를 거절하고,
  완성 조건 위반은 초안으로 보존한 채 알려 준다.
"""

from dataclasses import dataclass
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictStr

# PostgreSQL bigint 최댓값. 넘는 id는 저장 단계 DB 오류(500) 대신 입력 검증에서 거절한다.
BIGINT_MAX = 2**63 - 1

# 구조 참조(id)는 양의 정수 그대로만 받는다. "5"나 True 같은 값은 변환하지 않고 거절한다.
PositiveId = Annotated[int, Field(strict=True, gt=0, le=BIGINT_MAX)]


def _reject_nul(value: str) -> str:
    # PostgreSQL text는 NUL(\x00)을 저장할 수 없다. 다른 공백·제어 문자는 원문 그대로 둔다.
    if "\x00" in value:
        raise ValueError("NUL 문자는 사용할 수 없습니다.")
    return value


# 원문 텍스트. 자르거나 바꾸지 않고, DB에 저장할 수 없는 NUL만 거절한다.
RawText = Annotated[StrictStr, AfterValidator(_reject_nul)]


@dataclass(frozen=True)
class AuthorizedStory:
    """호출자가 이미 인가를 마친 스토리. 이 모듈은 사용자 인증·스토리 인가를 판단하지 않는다.

    owner_id는 이미지 소유 범위를 확인하는 데만 쓴다.
    """

    story_id: int
    owner_id: int


class PresetDraft(BaseModel):
    """프리셋 하나의 쓰기 입력. 이름·이미지·상세는 모두 비어 있어도 초안으로 저장할 수 있다."""

    model_config = ConfigDict(frozen=True)

    # 기존 프리셋 id. 없으면 새 프리셋이다. 수정·순서 변경 때 그대로 유지한다.
    id: PositiveId | None = None
    # 원문 이름. `{{user}}`도 치환하지 않고 그대로 보존한다.
    name: RawText | None = None
    # 이미 올라간 이미지 id. 업로드 경로는 CREATE-US-06 몫이다.
    image_id: PositiveId | None = None
    description: RawText | None = None


class PresetRecord(BaseModel):
    """저장된 프리셋 한 행. 응답 모델이 아니라 서비스 내부 반환값이다."""

    model_config = ConfigDict(frozen=True)

    id: int
    name: str | None
    image_id: int | None
    description: str | None
    position: int

    def to_draft(self) -> PresetDraft:
        return PresetDraft(
            id=self.id, name=self.name, image_id=self.image_id, description=self.description
        )


class PresetIssue(BaseModel):
    """완성 저장을 막는 입력 문제 하나. 초안 저장은 막지 않는다."""

    field: str = Field(description="예: presets, presets[0].name")
    code: str = Field(description="프론트 분기용 코드. 예: REQUIRED")
    message: str
    value: str | None = None


class PresetSaveResult(BaseModel):
    presets: list[PresetRecord]
    issues: list[PresetIssue]
