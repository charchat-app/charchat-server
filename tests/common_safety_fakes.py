"""CREATE-US-08-AC15 테스트용 중립 공통 안전 판정기(외부 호출 없음).

금지 콘텐츠 원문 대신 의미 없는 표식(`BLOCK_MARKER`)이 정본 어디에든 있으면 차단으로 본다.
"""

from collections.abc import Callable

from app.core.common_safety import CommonSafety, SafetySurface

BLOCK_MARKER = "ZQX-MARKER-7731"


class FakeGuard:
    def __init__(
        self,
        outcome=CommonSafety.CLEAR,
        *,
        on_call: Callable | None = None,
        marker: str = BLOCK_MARKER,
    ):
        self.outcome = outcome
        self.on_call = on_call
        self.marker = marker
        self.surfaces: list[SafetySurface] = []

    def __call__(self, surface: SafetySurface):
        self.surfaces.append(surface)
        if self.on_call is not None:
            self.on_call(surface)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        if self.marker in repr(surface):
            return CommonSafety.BLOCKED
        return self.outcome

    def texts(self) -> list[str]:
        return [repr(surface) for surface in self.surfaces]
