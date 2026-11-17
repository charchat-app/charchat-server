"""7번 공통 저장 중 2·3·4번 소유 영역과 저장 상태 계약."""

from typing import Annotated, Literal

from pydantic import AfterValidator, Field

from app.features.creation.initial_state.schemas import (
    InitialAffinity,
    InitialRelation,
    unique_affinities,
    unique_relations,
)
from app.features.creation.intro.schemas import Intro, IntroList
from app.features.creation.speech_style.fewshot_schemas import FewshotsResponse, FewshotsSaveRequest
from app.features.creation.speech_style.schemas import ExpressionFields

# 캐릭터 최대 10명 + 유저: 방향별 최대 11×10개.
RelationList = Annotated[
    list[InitialRelation], Field(max_length=110), AfterValidator(unique_relations)
]
# 캐릭터 최대 10명이 출발, 상대는 유저 + 다른 캐릭터 9명: 최대 10×10개.
AffinityList = Annotated[
    list[InitialAffinity], Field(max_length=100), AfterValidator(unique_affinities)
]


class CreationSaveRequest(FewshotsSaveRequest):
    expression: ExpressionFields
    # 누락을 허용하지 않아 실수로 전체 인트로를 삭제하지 않게 한다. 빈 배열은 전체 삭제다.
    intros: IntroList
    # CREATE-US-04 스토리 공통 초기 관계. 빈 배열은 전체 미지정이다.
    relations: RelationList
    # CREATE-US-04-AC09 방향별 초기 호감도. 관계와 독립이며 빈 배열은 모든 조합 0이다.
    affinities: AffinityList
    save_state: Literal["draft", "private", "public"]
    mode: Literal["manual", "auto"] = "manual"


class CreationSaveResponse(FewshotsResponse):
    expression: ExpressionFields
    intros: list[Intro]
    relations: list[InitialRelation]
    affinities: list[InitialAffinity]
