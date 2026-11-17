"""CREATE-US-01 요청·응답 스키마.

임시 저장은 미완성 입력도 보존해야 하므로 필수값은 스키마에서 막지 않는다.
완성 저장 조건 위반은 응답의 `issues`로 알려 준다.
"""

from datetime import datetime

from pydantic import BaseModel, Field


class StoryCreateRequest(BaseModel):
    title: str | None = None
    subtitle: str | None = Field(default=None, description="한 줄 소개")
    world_description: str | None = Field(default=None, description="세계관(서사)")
    tags: list[str] = Field(default_factory=list, description="해시태그 원문")


class StoryUpdateRequest(BaseModel):
    """기본 정보 전체 교체. `tags`가 null이면 기존 해시태그를 유지한다."""

    title: str | None = None
    subtitle: str | None = Field(default=None, description="한 줄 소개")
    world_description: str | None = Field(default=None, description="세계관(서사)")
    tags: list[str] | None = Field(default=None, description="해시태그 원문. null이면 유지")


class FieldIssue(BaseModel):
    """완성 저장(공개·비공개)을 막는 입력 문제 하나."""

    field: str = Field(description="문제가 있는 필드. 예: title")
    code: str = Field(description="프론트 분기용 코드. 예: REQUIRED")
    message: str


class StorySummaryResponse(BaseModel):
    """내 스토리 목록의 한 줄. 편집 화면을 다시 열기 위한 최소 정보만 담는다."""

    id: int
    save_state: str = Field(description="draft·private·public")
    title: str | None
    saved_at: datetime


class StoryResponse(BaseModel):
    id: int
    save_state: str = Field(description="draft·private·public")
    title: str | None
    subtitle: str | None
    world_description: str | None
    tags: list[str]
    saved_at: datetime
    issues: list[FieldIssue] = Field(
        description="완성 저장 조건 위반 목록. 비어 있으면 기본 정보 조건을 충족한다."
    )
