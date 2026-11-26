"""CHAT-US-13 대화 중 모델 변경 API 계약."""

from typing import Annotated

from pydantic import BaseModel, Field


class ModelResponse(BaseModel):
    # 공급자·공급자 쪽 모델 ID는 노출하지 않는다.
    id: int
    name: str


class ModelListResponse(BaseModel):
    models: list[ModelResponse]
    # CHAT_DEFAULT_MODEL_NAME과 이름이 같은 행 하나의 id. 없거나 여러 개면 null.
    default_model_id: int | None


class SessionModelChangeRequest(BaseModel):
    # 양의 정수만 받는다(문자열·실수·불리언은 요청 검증 오류).
    model_id: Annotated[int, Field(gt=0, strict=True)]


class ActiveGenerationModelResponse(BaseModel):
    id: int
    # 진행 중 생성이 요청한(끝까지 쓰는) 모델의 표시 이름.
    model: str


class SessionModelChangeResponse(BaseModel):
    model_id: int
    model: str
    # 바꾼 시점에 진행 중(pending·streaming)인 생성. 그 생성은 기존 모델로 끝까지 진행한다(AC02).
    active_generation: ActiveGenerationModelResponse | None
