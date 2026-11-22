"""7번 공통 저장 중 2·3·4번 소유 영역과 저장 상태 계약."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from app.features.creation.images.schemas import (
    MAX_CHARACTER_IMAGES,
    MAX_STORY_IMAGES,
    ImageItem,
)
from app.features.creation.initial_state.schemas import (
    MAX_AFFINITIES,
    MAX_RELATIONS,
    InitialAffinity,
    InitialRelation,
    unique_affinities,
    unique_relations,
)
from app.features.creation.intro.schemas import Intro, IntroList
from app.features.creation.speech_style.fewshot_schemas import (
    ClientKey,
    FewshotsResponse,
    FewshotsSaveRequest,
)
from app.features.creation.speech_style.schemas import ExpressionFields
from app.features.creation.story_basic.schemas import (
    MAX_CHARACTERS_PER_REQUEST,
    StoryUpdateRequest,
)
from app.features.creation.user_profile.schemas import PresetDraft

# 기본 정보 요청 상한(캐릭터 50명) + 유저 기준 방향쌍 수. 계산은 initial_state/schemas.py.
RelationList = Annotated[
    list[InitialRelation], Field(max_length=MAX_RELATIONS), AfterValidator(unique_relations)
]
AffinityList = Annotated[
    list[InitialAffinity], Field(max_length=MAX_AFFINITIES), AfterValidator(unique_affinities)
]
# CREATE-US-08: stories.requested_rating·effective_rating과 같은 값. 대소문자를 바꾸지 않는다.
Rating = Literal["SAFE", "UNSAFE"]
RatingReviewStatus = Literal["not_reviewed", "reviewing", "completed", "failed"]


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
    # CREATE-US-07: 1번 기본 정보(선택). 모양은 기본 정보 수정 요청과 같다.
    # 비공개·공개 스토리의 임시 저장은 원문 그대로 임시 저장본에 두고, 빼고 보내면 이전에 임시
    # 저장한 기본 정보를 유지한다. 저장은 보낸 값, 없으면 임시 저장한 값을 함께 검사·반영한다.
    # 미등록 스토리는 다른 영역처럼 본 테이블에 바로 쓴다.
    basic: StoryUpdateRequest | None = None
    # CREATE-US-05: 5번 유저 프리셋 전체 목록(선택). 목록 순서가 편집 순서다.
    # 빼거나 null이면 기존 값(임시 저장한 값 또는 저장본)을 유지하고, []는 모두 비운다.
    # 비공개·공개 스토리의 임시 저장은 원문 그대로 임시 저장본에만 둔다.
    presets: list[PresetDraft] | None = None
    # CREATE-US-07-AC07: 「저장」의 캐릭터 얼굴·스토리 표지 이미지 목록(선택).
    # 빼거나 null이면 통합 전과 같다. 이번 `basic`의 새 캐릭터는 이 목록으로 사진을 연결한다.
    # CREATE-US-07-AC09: 비공개·공개 스토리의 preserve_scene 임시 저장은 `basic_index` 사진을
    # 그 `basic`과 짝지어 공통 임시 저장본에만 두고, 「저장」에서 새 캐릭터에 연결한다.
    images: "CreationImagesInput | None" = None
    # CREATE-US-07-AC02·AC03·AC07, CREATE-US-05: 2~4번 장면(퓨샷·문체·인트로·관계·호감도)을
    # 맡지 않는 편집 화면의 쓰기·재시도용(선택, 기본 false). true면 보낸 장면 필드는 형식만
    # 맞으면 되고 쓰지 않는다. 서버가 스토리 잠금 안에서 최신 장면(임시 저장본이 있으면 그
    # 원문, 없으면 저장본)을 골라 쓴다. 요청 해시에도 보낸 장면 내용을 넣지 않는다.
    # false(생략)면 통합 전과 같은 요청·해시다.
    preserve_scene: bool = False
    # CREATE-US-07-AC09: 확정할 공통 자동 저장의 요청키(선택).
    # 자동 저장 응답의 `finalize_auto_request_id`를 그대로 보낸다.
    # 그 자동 저장 뒤로 저장본·임시 저장본이 그대로일 때만 임시 저장본
    # 전체를 반영하고, 바뀌었으면 409 CREATION_AUTOSAVE_CHANGED다.
    # preserve_scene=true인 「저장」(kind=save, mode=manual)에서만 쓴다.
    # 반영할 내용은 임시 저장본에서 고르므로 basic·presets·images는
    # 함께 보낼 수 없다. 생략·null은 기존 요청·해시와 같다.
    finalize_auto_request_id: ClientKey | None = None
    # CREATE-US-08-AC01·AC13: 제작자가 고른 스토리 등급(선택). 빼면 기존 선택(임시 저장한 값
    # 또는 저장본)을 유지하고, 보낼 때는 SAFE·UNSAFE만 받는다(null 거절). 생략하면 기존 요청·
    # 해시와 같다. 실제 적용 등급(effective_rating)·검토 상태는 요청으로 바꿀 수 없다(보내면
    # extra=forbid로 422).
    requested_rating: Rating | None = None
    # CREATE-US-08-AC13: 등급만 담은 임시 저장본을 반영할 때 그 응답의 `working_copy_saved_at`을
    # 그대로 보낸다(선택). preserve_scene 「저장」(kind=save, mode=manual)에서만 쓰고,
    # basic·presets·images·finalize_auto_request_id와 함께 보낼 수 없다. requested_rating은
    # 함께 보내 임시 저장한 선택을 바꿀 수 있다. 스토리 잠금 안에서 임시 저장본이 그 시각
    # 그대로 등급만 담고, 공개 상태가 같고, 사진 임시 저장본이 없을 때만 반영하고, 아니면
    # 409 CREATION_RATING_WORKING_COPY_CHANGED다. 생략·null은 기존 요청·해시와 같다.
    finalize_rating_working_copy_at: datetime | None = None

    @field_validator("requested_rating", mode="before")
    @classmethod
    def reject_null_rating(cls, value):
        # 기본값(생략)에는 검사기가 돌지 않으므로, 여기 오는 None은 직접 보낸 null이다.
        if value is None:
            raise ValueError("requested_rating은 SAFE 또는 UNSAFE로 보내거나 빼야 합니다.")
        return value

    @model_validator(mode="after")
    def check_finalize_autosave(self) -> "CreationSaveRequest":
        if self.finalize_auto_request_id is None:
            return self
        if self.kind != "save" or self.mode != "manual" or not self.preserve_scene:
            raise ValueError("자동 저장 확정은 preserve_scene 「저장」에서만 쓴다.")
        if self.basic is not None or self.presets is not None or self.images is not None:
            raise ValueError("자동 저장 확정에는 basic·presets·images를 보낼 수 없다.")
        if self.requested_rating is not None:
            # 확정은 자동 저장한 내용만 반영한다. 등급 선택도 임시 저장본에서 고른다.
            raise ValueError("자동 저장 확정에는 requested_rating을 보낼 수 없다.")
        return self

    @model_validator(mode="after")
    def check_finalize_rating(self) -> "CreationSaveRequest":
        if self.finalize_rating_working_copy_at is None:
            return self
        if self.kind != "save" or self.mode != "manual" or not self.preserve_scene:
            raise ValueError("등급 임시 저장본 반영은 preserve_scene 「저장」에서만 쓴다.")
        if self.basic is not None or self.presets is not None or self.images is not None:
            raise ValueError("등급 임시 저장본 반영에는 basic·presets·images를 보낼 수 없다.")
        if self.finalize_auto_request_id is not None:
            raise ValueError("등급 임시 저장본 반영과 자동 저장 확정은 함께 쓸 수 없다.")
        return self

    @model_validator(mode="after")
    def check_image_references(self) -> "CreationSaveRequest":
        """이미지 참조가 가리키는 캐릭터를 요청 안에서 하나로 정할 수 있는지 확인한다."""
        if self.images is None:
            return self

        roster = self.basic.characters if self.basic is not None else None
        entries = self.images.characters
        indexes = [e.basic_index for e in entries if e.basic_index is not None]
        temporary = self.kind == "temporary" or self.mode == "auto"
        # CREATE-US-07-AC09: 임시 저장의 새 캐릭터 사진은 preserve_scene 공통 화면에서만 받는다.
        # 미등록 스토리는 서비스가 거절한다(임시 저장도 본 테이블에 바로 써 새 ID가 생긴다).
        if indexes and temporary and not self.preserve_scene:
            raise ValueError(
                "새 캐릭터의 이미지 연결은 「저장」 또는 공통 임시 저장에서만 보낼 수 있습니다."
            )
        ids = [e.character_id for e in entries if e.character_id is not None]
        if len(set(indexes)) != len(indexes) or len(set(ids)) != len(ids):
            raise ValueError("같은 캐릭터의 이미지 목록을 두 번 보낼 수 없습니다.")
        if indexes and roster is None:
            raise ValueError("basic_index에는 basic.characters가 함께 필요합니다.")
        for index in indexes:
            if index >= len(roster) or roster[index].id is not None:
                raise ValueError(f"basic_index {index}는 새 캐릭터(id null)가 아닙니다.")
        if roster is not None:
            listed = {c.id for c in roster if c.id is not None}
            if not set(ids) <= listed:
                raise ValueError("character_id가 basic.characters에 없습니다.")
            given = set(indexes)
            missing = [i for i, c in enumerate(roster) if c.id is None and i not in given]
            # 임시 저장은 일부 새 캐릭터만 사진을 담아도 원문으로 둔다. 「저장」이 완성 검사한다.
            if missing and not temporary:
                raise ValueError(f"새 캐릭터 {missing}의 basic_index 목록이 없습니다.")
        return self


ImageId = Annotated[int, Field(gt=0, le=9223372036854775807)]


class CreationCharacterImagesInput(BaseModel):
    """캐릭터 하나의 얼굴 이미지 목록 전체. 순서가 곧 position이다(CREATE-US-06-AC06).

    대상은 둘 중 하나만 쓴다.
    - `character_id`: 이미 저장된 캐릭터
    - `basic_index`: 이번 요청 `basic.characters`에서 id가 null인 새 캐릭터의 위치(0부터)
    """

    model_config = ConfigDict(extra="forbid")

    character_id: int | None = Field(default=None, gt=0, le=9223372036854775807)
    basic_index: int | None = Field(default=None, ge=0, lt=MAX_CHARACTERS_PER_REQUEST)
    image_ids: list[ImageId] = Field(max_length=MAX_CHARACTER_IMAGES)

    @model_validator(mode="after")
    def check_target(self) -> "CreationCharacterImagesInput":
        if (self.character_id is None) == (self.basic_index is None):
            raise ValueError("character_id와 basic_index 중 하나만 보내야 합니다.")
        if any(i <= 0 for i in self.image_ids):
            raise ValueError("이미지 ID는 양의 정수여야 합니다.")
        if len(set(self.image_ids)) != len(self.image_ids):
            raise ValueError("캐릭터 이미지 목록에 같은 이미지를 두 번 넣을 수 없습니다.")
        return self


class CreationImagesInput(BaseModel):
    """「저장」에 함께 반영할 이미지. 담지 않은 캐릭터의 이미지는 그대로 둔다.

    `story_image_ids`(CREATE-US-07-AC07·AC08)는 스토리 표지 이미지 목록 전체이며 순서가 곧
    position이다(첫 이미지가 대표, CREATE-US-06-AC06). 빼거나 null이면 지금 목록(사진 임시
    저장본을 반영한 결과)을 그대로 두고, []는 모두 비운다. 둘 중 하나는 보내야 한다.
    """

    model_config = ConfigDict(extra="forbid")

    characters: list[CreationCharacterImagesInput] = Field(
        default_factory=list, max_length=MAX_CHARACTERS_PER_REQUEST
    )
    story_image_ids: list[ImageId] | None = Field(default=None, max_length=MAX_STORY_IMAGES)

    @model_validator(mode="after")
    def check_story_images(self) -> "CreationImagesInput":
        if not self.characters and self.story_image_ids is None:
            raise ValueError("characters 또는 story_image_ids 중 하나는 보내야 합니다.")
        ids = self.story_image_ids or []
        if any(i <= 0 for i in ids):
            raise ValueError("이미지 ID는 양의 정수여야 합니다.")
        if len(set(ids)) != len(ids):
            raise ValueError("스토리 이미지 목록에 같은 이미지를 두 번 넣을 수 없습니다.")
        return self


CreationSaveRequest.model_rebuild()


class PresetResponse(BaseModel):
    """편집 화면에 보여 줄 프리셋 하나. 임시 저장본이면 원문이고 새 프리셋은 id가 null이다."""

    id: int | None = None
    name: str | None = None
    image_id: int | None = None
    description: str | None = None
    # 읽기 전용 미리보기 URL. 응답할 때 소유자 이미지만 찾아 채우고, 남의·없는 이미지면 null이다.
    # 요청(PresetDraft)·임시 저장본 JSON에는 담지 않는다. 이 필드가 생기기 전에 기록된 요청키
    # 응답을 재전송할 때도 null로 채운다.
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
    # CREATE-US-07: 아직 저장(반영)하지 않은 임시 저장 기본 정보(원문). 없으면 null이며,
    # 저장된 기본 정보는 기본 정보 조회(GET /stories/{id})로 본다.
    basic: StoryUpdateRequest | None = None
    # CREATE-US-05: 임시 저장한 프리셋이 있으면 그 원문(presets_pending=true), 없으면 저장본.
    # 프리셋 통합 전에 기록된 요청키 응답을 재전송할 때는 기본값([]·false)으로 채운다.
    presets: list[PresetResponse] = Field(default_factory=list)
    presets_pending: bool = False
    # CREATE-US-07-AC09: 이 공통 자동 저장을 「저장」으로 확정할 수 있으면
    # 자기 요청키, 아니면 null. 앞에 임시 저장본이 없었거나, 앞의 임시
    # 저장본이 아직 바뀌지 않은 확정 가능 자동 저장일 때만 값이 있다.
    # 이 필드 전에 기록된 응답을 재전송할 때는 null로 채운다.
    finalize_auto_request_id: str | None = None
    # CREATE-US-07-AC09: 임시 저장한 `basic`의 새 캐릭터(id null) 사진. `basic_index`는 그
    # `basic.characters` 위치다. 항목 모양은 사진 API(`/images`)와 같다. 없으면 [].
    # 이 필드 전에 기록된 응답을 재전송할 때도 []로 채운다.
    staged_character_images: list["StagedCharacterImagesResponse"] = Field(default_factory=list)
    # CREATE-US-08-AC01·AC13: 편집 화면의 등급 선택. 임시 저장한 선택이 있으면 그 값,
    # 없으면 저장본(stories.requested_rating)이다.
    requested_rating: Rating = "SAFE"
    # CREATE-US-08: 실제 적용 등급과 검토 상태(저장본). 임시 저장·저장으로 바뀌지 않는다.
    # 등급 필드 전에 기록된 요청키 응답을 재전송할 때는 컬럼 기본값으로 채운다.
    effective_rating: Rating = "SAFE"
    rating_review_status: RatingReviewStatus = "not_reviewed"
    # CREATE-US-08-AC13: 2~5번 임시 저장본이 등급 선택만 담았고 사진 임시 저장본이 없으면 true.
    # 이때만 `finalize_rating_working_copy_at`으로 반영할 수 있다. 이 필드 전 응답은 false로 채운다.
    rating_only_working_copy: bool = False


class StagedCharacterImagesResponse(BaseModel):
    """임시 저장한 새 캐릭터 하나의 사진 목록. 순서가 곧 position이다."""

    basic_index: int
    images: list[ImageItem]


CreationSaveResponse.model_rebuild()


class FieldIssue(BaseModel):
    """완성 저장(공개·비공개)을 막는 입력 문제 하나. 기본 정보 API의 FieldIssue와 같은 형식이다."""

    field: str = Field(description="문제가 있는 필드. 예: intros[2].messages, tags[0]")
    code: str = Field(description="프론트 분기용 코드. 예: INTRO_TOO_LONG")
    message: str
    value: str | None = Field(
        default=None,
        description="길이 초과는 초과 글자 수, 해시태그 문제는 태그 원문. 그 밖에는 null.",
    )
