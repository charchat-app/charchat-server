"""7번 공통 저장 중 2·3번 소유 영역과 저장 상태 계약."""

from typing import Literal

from pydantic import BaseModel, Field

from app.features.creation.intro.schemas import Intro, IntroList
from app.features.creation.speech_style.fewshot_schemas import FewshotsResponse, FewshotsSaveRequest
from app.features.creation.speech_style.schemas import ExpressionFields


class CreationSaveRequest(FewshotsSaveRequest):
    expression: ExpressionFields
    # 누락을 허용하지 않아 실수로 전체 인트로를 삭제하지 않게 한다. 빈 배열은 전체 삭제다.
    intros: IntroList
    save_state: Literal["draft", "private", "public"]
    mode: Literal["manual", "auto"] = "manual"


class CreationSaveResponse(FewshotsResponse):
    expression: ExpressionFields
    intros: list[Intro]


class FieldIssue(BaseModel):
    """완성 저장(공개·비공개)을 막는 입력 문제 하나. 기본 정보 API의 FieldIssue와 같은 형식이다."""

    field: str = Field(description="문제가 있는 필드. 예: intros[2].messages, tags[0]")
    code: str = Field(description="프론트 분기용 코드. 예: INTRO_TOO_LONG")
    message: str
    value: str | None = Field(
        default=None,
        description="길이 초과는 초과 글자 수, 해시태그 문제는 태그 원문. 그 밖에는 null.",
    )
