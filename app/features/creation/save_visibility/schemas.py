"""7번 공통 저장 중 2·3번 소유 영역과 저장 상태 계약."""

from typing import Literal

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
