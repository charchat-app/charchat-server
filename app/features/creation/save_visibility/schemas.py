"""7번 공통 저장 중 2·3·4·5번 소유 영역과 저장 상태 계약."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field

from app.features.creation.initial_state.schemas import (
    MAX_AFFINITIES,
    MAX_RELATIONS,
    InitialAffinity,
    InitialRelation,
    unique_affinities,
    unique_relations,
)
from app.features.creation.intro.schemas import Intro, IntroList
from app.features.creation.speech_style.fewshot_schemas import FewshotsResponse, FewshotsSaveRequest
from app.features.creation.speech_style.schemas import ExpressionFields
from app.features.creation.user_profile.schemas import PresetDraft

# 기본 정보 요청 상한(캐릭터 50명) + 유저 기준 방향쌍 수. 계산은 initial_state/schemas.py.
RelationList = Annotated[
    list[InitialRelation], Field(max_length=MAX_RELATIONS), AfterValidator(unique_relations)
]
AffinityList = Annotated[
    list[InitialAffinity], Field(max_length=MAX_AFFINITIES), AfterValidator(unique_affinities)
]


class CreationSaveRequest(FewshotsSaveRequest):
    expression: ExpressionFields
    # 누락을 허용하지 않아 실수로 전체 인트로를 삭제하지 않게 한다. 빈 배열은 전체 삭제다.
    intros: IntroList
    # CREATE-US-04 스토리 공통 초기 관계. 빈 배열은 전체 미지정이다.
    relations: RelationList
    # CREATE-US-04-AC09 방향별 초기 호감도. 관계와 독립이며 빈 배열은 모든 조합 0이다.
    affinities: AffinityList
    # 공개 상태. draft는 미등록이다(2026-11-20 결정: 공개 상태와 저장 상태를 나눈다).
    save_state: Literal["draft", "private", "public"]
    # 저장 상태. temporary(임시 저장)는 이용자 화면을 바꾸지 않고, save(저장)는 반영한다.
    # 비공개·공개 스토리의 임시 저장은 임시 저장본에만 두고, 미등록 스토리는 본 테이블에 바로 쓴다.
    kind: Literal["temporary", "save"] = "save"
    # auto(자동 저장)는 언제나 임시 저장이다.
    mode: Literal["manual", "auto"] = "manual"
    # CREATE-US-05: 5번 유저 프리셋 전체 목록(선택). 목록 순서가 편집 순서다.
    # 빼거나 null이면 기존 값(임시 저장한 값, 없으면 저장본)을 유지하고, []는 모두 비운다.
    # 비공개·공개 스토리의 임시 저장은 원문 그대로 임시 저장본에만 둔다.
    presets: list[PresetDraft] | None = None
    # 프리셋 화면은 2~4번 장면을 편집하지 않는다. 서버 잠금 안에서 최신 장면을 사용한다.
    preserve_scene: bool = False


class PresetResponse(BaseModel):
    """편집 화면에 보여 줄 프리셋 하나. 임시 저장본이면 원문이고 새 프리셋은 id가 null이다."""

    id: int | None = None
    name: str | None = None
    image_id: int | None = None
    description: str | None = None
    # 읽기 전용 미리보기 URL. 응답할 때 소유자 이미지만 찾아 채우고, 남의·없는 이미지면 null이다.
    # 요청(PresetDraft)·임시 저장본 JSON에는 담지 않는다.
    image_url: str | None = None


class CreationSaveResponse(FewshotsResponse):
    expression: ExpressionFields
    intros: list[Intro]
    relations: list[InitialRelation]
    affinities: list[InitialAffinity]
    # 값이 있으면 위 내용은 아직 저장(반영)하지 않은 임시 저장본이다. 이용자는 이전 저장본을 본다.
    working_copy_saved_at: datetime | None = None
    # 값이 있으면 6번 사진에 아직 반영하지 않은 임시 저장본이 있다. 「저장」하면 함께 반영한다.
    images_working_copy_saved_at: datetime | None = None
    # CREATE-US-05: 임시 저장한 프리셋이 있으면 그 원문(presets_pending=true), 없으면 저장본.
    # 프리셋 연결 전에 기록된 요청키 응답을 재전송할 때는 기본값([]·false)으로 채운다.
    presets: list[PresetResponse] = Field(default_factory=list)
    presets_pending: bool = False


class FieldIssue(BaseModel):
    """완성 저장(공개·비공개)을 막는 입력 문제 하나. 기본 정보 API의 FieldIssue와 같은 형식이다."""

    field: str = Field(description="문제가 있는 필드. 예: intros[2].messages, tags[0]")
    code: str = Field(description="프론트 분기용 코드. 예: INTRO_TOO_LONG")
    message: str
    value: str | None = Field(
        default=None,
        description="길이 초과는 초과 글자 수, 해시태그 문제는 태그 원문. 그 밖에는 null.",
    )
