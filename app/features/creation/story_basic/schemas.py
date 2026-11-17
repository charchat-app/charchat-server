"""CREATE-US-01 요청·응답 스키마.

임시 저장은 미완성 입력도 보존해야 하므로 필수값은 스키마에서 막지 않는다.
완성 저장 조건 위반은 응답의 `issues`로 알려 준다.
"""

from datetime import datetime

from pydantic import BaseModel, Field

# 요청 하나에 담을 수 있는 캐릭터 수의 하드 상한. 등록 조건(1~10명)과 별개다.
# 임시 저장은 조건을 넘는 입력도 보존해야 하므로 넉넉하게 두고, 과도한 요청만 형식 오류로 막는다.
MAX_CHARACTERS_PER_REQUEST = 50


class CharacterWriteItem(BaseModel):
    """캐릭터 한 명. 목록 순서가 곧 표시 순서다."""

    id: int | None = Field(default=None, description="기존 캐릭터 id. 새 캐릭터는 null")
    name: str | None = None
    description: str | None = Field(default=None, description="성격·외형·배경 자유 서술")


class StoryCreateRequest(BaseModel):
    title: str | None = None
    subtitle: str | None = Field(default=None, description="한 줄 소개")
    world_description: str | None = Field(default=None, description="세계관(서사)")
    tags: list[str] = Field(default_factory=list, description="해시태그 원문")
    characters: list[CharacterWriteItem] = Field(
        default_factory=list,
        max_length=MAX_CHARACTERS_PER_REQUEST,
        description="새 캐릭터 목록. id는 넣지 않는다",
    )


class StoryUpdateRequest(BaseModel):
    """기본 정보 전체 교체.

    - `tags`가 null이면 기존 해시태그를 유지한다.
    - `characters`가 null이면 기존 캐릭터를 유지한다. 목록을 보내면 그 목록이 전체 캐릭터가 된다.
      목록에 없는 기존 캐릭터는 삭제되고, id가 없는 항목은 새로 추가된다.
    """

    title: str | None = None
    subtitle: str | None = Field(default=None, description="한 줄 소개")
    world_description: str | None = Field(default=None, description="세계관(서사)")
    tags: list[str] | None = Field(default=None, description="해시태그 원문. null이면 유지")
    characters: list[CharacterWriteItem] | None = Field(
        default=None,
        max_length=MAX_CHARACTERS_PER_REQUEST,
        description="전체 캐릭터 목록. null이면 유지",
    )


class FieldIssue(BaseModel):
    """완성 저장(공개·비공개)을 막는 입력 문제 하나."""

    field: str = Field(description="문제가 있는 필드. 예: title, characters[0].name")
    code: str = Field(description="프론트 분기용 코드. 예: REQUIRED")
    message: str


class StorySummaryResponse(BaseModel):
    """내 스토리 목록의 한 줄. 편집 화면을 다시 열기 위한 최소 정보만 담는다."""

    id: int
    save_state: str = Field(description="draft·private·public")
    title: str | None
    saved_at: datetime


class CharacterResponse(BaseModel):
    id: int
    name: str | None
    description: str | None
    position: int = Field(description="표시 순서. 0부터")


class StoryResponse(BaseModel):
    id: int
    save_state: str = Field(description="draft·private·public")
    title: str | None
    subtitle: str | None
    world_description: str | None
    tags: list[str]
    characters: list[CharacterResponse] = Field(description="표시 순서대로 정렬")
    saved_at: datetime
    issues: list[FieldIssue] = Field(
        description="완성 저장 조건 위반 목록. 비어 있으면 기본 정보·캐릭터 조건을 충족한다."
    )
