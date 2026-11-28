"""CREATE-US-08-AC02·AC14 기본 앱 등급 검토 조정기 구성(설정으로 켤 때만).

- `app/main.py`의 `create_app`이 `RATING_REVIEW_ENABLED`를 켰고 조정기를 주입하지 않았을 때만
  쓴다. 판정 정책·조정기·평가기 동작은 바꾸지 않고, 기존 구현(`JevDecisionsEvaluator`·
  `DatabaseReviewResults`)을 엮기만 한다. 공통 안전 판정기(AC15)는 여기서 만들지 않는다.
- 오류 메시지에는 환경변수 이름만 싣고 값(키·접속 정보)은 싣지 않는다.
- 종료(`close_coordinator`)는 대기열·평가 중 검토를 모두 끝낸 뒤(`drain`) DB 풀보다 먼저 닫는다.
  전체 대기 상한은 없다(아래 설명). 결과 쓰기 연결에는 앱 소유 시간 제한을 건다.
"""

import asyncio
import logging

from pydantic import SecretStr

from app.features.creation.save_visibility.jev_decisions import REVISION, JevDecisionsEvaluator
from app.features.creation.save_visibility.jev_http_transport import JevHttpTransport
from app.features.creation.save_visibility.rating_review import (
    DatabaseReviewResults,
    RatingReviewCoordinator,
)

logger = logging.getLogger(__name__)

# AC14 영속 통과 재사용 이름공간 정책 개정. `jev_decisions`의 질문·기준·판정 결합 방식이 바뀌면
# 올린다(예전 통과 지문은 이름공간이 달라 재사용되지 않는다).
REUSE_POLICY_REVISION = 1
# 평가기(Jev Decisions)·모델 개정·정책 개정을 가리키는 고정 이름공간. 원문을 담지 않는다.
REUSE_NAMESPACE = f"jev-decisions.{REVISION.replace('/', '-')}.p{REUSE_POLICY_REVISION}"
# 앱이 소유한 결과 쓰기 연결의 시간 제한(초). 결과 한 건을 쓰는 연결·트랜잭션에만 건다(전역 DB
# 설정이나 요청용 풀은 바꾸지 않는다). 임시 기본값이다(지연·재시도 정책은 미정, 상세 문서 2326529).
RESULT_CONNECT_TIMEOUT_SECONDS = 5.0
RESULT_LOCK_TIMEOUT_SECONDS = 5.0
RESULT_STATEMENT_TIMEOUT_SECONDS = 10.0


def jev_transport(api_key: SecretStr | None) -> JevHttpTransport:
    """설정의 Jev 키로 전송을 만든다. 없거나 형식이 틀리면 값 없이 시작을 거부한다."""
    value = api_key.get_secret_value() if isinstance(api_key, SecretStr) else None
    if not value:
        raise RuntimeError("JEV_OPENROUTER_API_KEY를 설정해야 합니다.")
    try:
        return JevHttpTransport(value)
    except ValueError:
        pass
    # except 블록 밖에서 올려 원인 예외가 붙지 않게 한다.
    raise RuntimeError("JEV_OPENROUTER_API_KEY 형식이 올바르지 않습니다.")


def rating_review_coordinator(
    transport: JevHttpTransport, database_url: str
) -> RatingReviewCoordinator:
    """AC02·AC14 앱이 소유하는 등급 검토 조정기(결과 DB 저장·영속 통과 재사용)."""
    return RatingReviewCoordinator(
        JevDecisionsEvaluator(transport),
        results=DatabaseReviewResults(
            database_url,
            connect_timeout=RESULT_CONNECT_TIMEOUT_SECONDS,
            lock_timeout=RESULT_LOCK_TIMEOUT_SECONDS,
            statement_timeout=RESULT_STATEMENT_TIMEOUT_SECONDS,
        ),
        reuse_namespace=REUSE_NAMESPACE,
    )


async def close_coordinator(coordinator: RatingReviewCoordinator) -> None:
    """앱이 소유한 조정기의 정상 종료. 대기열·평가 중 검토를 취소하지 않고 모두 끝낸다.

    `drain()`은 막히는 호출이라 이벤트 루프를 막지 않도록 별도 스레드에서 기다린다. 전체 대기
    시간에는 상한이 없다(검토 하나는 평가기 마감·울타리 대기·결과 DB 시간 제한으로 끝나지만 대기열
    길이만큼 늘어난다). 서버의 종료 유예 시간이 먼저 끝나거나 프로세스가 강제 종료되면 이 대기는
    끝까지 가지 못하며, 남은 검토는 다음 확정 저장까지 `reviewing`으로 남는다. 이 대기가 취소돼도
    작업자 스레드는 멈추지 않는다(결과 쓰기는 요청용 풀이 아닌 독립 연결을 쓴다).
    """
    logger.info("등급 검토 종료 대기 시작(대기열·평가 중 검토를 끝낸 뒤 닫는다)")
    await asyncio.to_thread(coordinator.drain)
    logger.info("등급 검토 종료 대기 완료")
