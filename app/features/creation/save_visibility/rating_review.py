"""CREATE-US-08-AC02·AC14 UNSAFE 확정 저장 뒤 등급 검토 넘김(선택, 기본 꺼짐).

- 비공개·공개 「저장」이 커밋되면 그 저장본을 조정기에 알린다. 저장본 등급 선택이 UNSAFE면
  검토 비교 내용을 백그라운드 평가기로 넘기고, SAFE이거나 내용을 읽지 못했으면(None) 앞서
  넘긴 검토만 무효로 한다. 요청은 평가를 기다리지 않는다.
- 커밋과 알림 사이 경쟁: 확정 저장은 스토리 잠금 안, COMMIT 직전 마지막 단계에 조정기
  울타리(`fence`)에 들어가고, 트랜잭션이 끝난 뒤 울타리를 나온다. 커밋에 성공했을 때만
  가장 최근 세대와 내용을 알린다(커밋 실패는 앞선 검토를 무효로 하지 않는다). 작업자는
  평가 시작 전과 통과 기록 전에 그 스토리의 울타리가 닫힐 때까지 기다린 뒤 자기 내용이
  아직 최신인지 확인한다. 평가 중에는 어떤 잠금도 쥐지 않는다.
- 단일 프로세스 전제다. 여러 프로세스·서버 사이의 순서는 보장하지 않는다.
- 평가기는 주입한다. 마지막 통과 기록은 이 프로세스 메모리에만 두며(재시작하면 사라진다)
  조정기 잠금 안에서 읽고 쓴다. 지연·실패·재시도 정책은 미정(상세 문서 2326529)이다. 기본 앱은
  `RATING_REVIEW_ENABLED`를 켰을 때만 Jev 평가기로 조정기를 만든다(`runtime`, 기본 꺼짐).
- 결과 보관소(`results`)를 주지 않으면 DB에 검토 결과를 쓰지 않고 적용 등급·검토 상태도 바꾸지
  않는다(기존 동작). 보관소는 테스트·수동 주입과 설정으로 켠 기본 앱 조정기에서만 준다
  (아래 「검토 결과 저장」).
- 스토리 원문을 로그에 남기지 않는다(스토리 ID·세대·예외 종류만).

검토 결과 저장(CREATE-US-08-AC02·AC03·AC09~AC11, `results`를 줄 때만):
- 확정 저장 트랜잭션 안, 응답을 만들기 전에 UNSAFE면 검토 중(`reviewing`)과 새 요청 표
  (`rating_requested_at`)를 쓴다. 적용 등급은 그대로 둔다(재검토 중에는 앞선 UNSAFE 유지).
  SAFE면 적용 등급을 SAFE·검토 상태를 not_reviewed로 둬 대기 중인 검토를 무효로 한다. 요청 표는
  마지막 요청 시각으로 남겨 다음 요청 표가 늘 더 크게 한다(앞선 표를 다시 쓰지 않는다).
- 작업자는 평가를 마친 뒤 조정기 잠금 밖에서 독립 DB 연결로 스토리를 잠그고, 요청 표가 그대로이고
  UNSAFE 선택·검토 중·삭제되지 않았으며 정본 내용이 평가한 내용과 같을 때만 결과를 쓴다.
- 적격은 UNSAFE·완료, 확인된 부적격·판단 불가는 SAFE·완료(사유 코드 기록), 평가 실패·잘못된
  출력·넘김 실패는 앞선 적용 등급을 유지한 채 실패로 기록한다(부적격이 아니다). 사유는 허용
  목록 코드만 쓰고 평가기 원문은 쓰지 않는다. 새 재시도 정책은 없다(다음 확정 저장이 다시 넘긴다).
- 이 경로는 프로세스 메모리 통과 기록·중복 제거를 쓰지 않는다. 메모리 기록은 영속하지 않고 다른
  경로로 정본이 바뀌었는지 알 수 없어 잘못 재사용할 수 있다. 앞선 요청의 평가는 요청 표가 달라
  결과를 쓰지 못한다.

영속 마지막 통과 재사용(CREATE-US-08-AC14, `results`와 `reuse_namespace`를 함께 줄 때만):
- 통과 지문(`stories.rating_pass_digest`)은 정본 검토 비교 내용의 버전 붙은 SHA-256 해시만
  담는다(원문 없음). `v<개정>:<이름공간>:<내용 해시>` 형식이며 이름공간은 평가기(공급자·모델·
  정책)를 가리킨다. 이름공간이나 개정이 다르면 문자열이 달라 재사용하지 않는다(암묵적 공급자 간
  재사용 없음). 비교 계약(내용 구성·인코딩)이 바뀌면 `PASS_DIGEST_REVISION`을 올린다.
- 지문은 결과 쓰기와 같은 스토리 잠금·같은 UPDATE에서, 요청 표가 그대로인 현재 검토가 적격으로
  끝날 때만 쓴다. 이름공간이 없는 적격 완료는 지문을 비운다(마지막 통과를 알 수 없음). 부적격·판단
  불가 완료는 같은 내용의 지문만 비우고, 실패·결과 없음은 지문을 바꾸지 않는다(다른 내용의 예전
  통과를 덮어쓰지 않는다).
- 지문을 모르는 이전 버전 작성기는 거부 완료 때 지문을 그대로 둔다. 그래서 결과 쓰기는 같은
  트랜잭션에 작성기 표시를 두고, DB 트리거는 표시 없는 검토 중 → 거부 완료(SAFE) 전환에서 남은
  지문을 비운다(이전 버전이 같은 내용을 거부한 뒤 재사용하지 않는다).
- 확정 저장 트랜잭션(스토리 잠금 안)에서 방금 쓴 정본 내용의 지문이 저장된 지문과 정확히 같으면
  평가기를 부르지 않고 적용 등급 UNSAFE·완료와 새 요청 표를 같은 트랜잭션에 쓴다. 새 요청 표와
  `completed` 상태가 다른 프로세스의 대기·평가 중 결과를 막고(DB 울타리), 조정기는 그 스토리의 최신
  내용을 검토 대상 아님으로 알려 이 프로세스의 대기 검토도 평가하지 않게 한다(프로세스 울타리).
  임시 저장본은 읽지 않으며 메모리 값은 판단 근거로 쓰지 않는다.
- 종료: 앱이 소유한 조정기는 `drain()`으로 대기열·평가 중 검토를 모두 끝낸 뒤 닫는다(취소하지
  않음, 전체 대기 상한 없음). 주입한 조정기와 기존 호출자는 `close()`(즉시, 대기열 취소)를 쓴다.
- 남은 한계: `close()`·강제 종료로 검토가 사라지면 그 스토리는 다음 확정 저장까지
  `reviewing`(앞선 적용 등급 유지)으로 남는다. 다른 경로로 정본이 바뀌어 내용이 어긋난 결과도
  쓰지 않으므로 같은 상태로 남는다. 재시도·복구 작업(영속 대기열)은 미정이다.
"""

import hashlib
import itertools
import json
import logging
import math
import re
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from typing import Protocol

import psycopg
from psycopg.rows import dict_row
from pydantic import ValidationError

from app.features.creation.initial_state import service as initial_state
from app.features.creation.save_visibility import repository
from app.features.creation.save_visibility.review_verdict import (
    AttributedVerdict,
    Eligibility,
    EligibilityReason,
    EligibilityVerdict,
    ReviewTarget,
    TargetKind,
    revalidated,
    unavailable,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReviewSnapshot:
    """AC14 검토 비교 내용. 내용이 같으면 같다(요청 원문 해시가 아니다).

    원문을 담으므로 repr에 싣지 않는다.
    """

    world_description: str | None = field(repr=False)
    # (캐릭터 id, 이름, 상세) id 순. 세계관·상세·관계가 이름으로 인물을 가리키므로 이름도
    # 인물 정체로 비교한다. 그 밖의 필드는 비교하지 않는다.
    characters: tuple = field(repr=False)
    # (출발 종류, 출발 id, 대상 종류, 대상 id, 라벨) 정렬. 라벨이 빈 관계는 뺀다.
    relations: tuple = field(repr=False)


def build_snapshot(world_description, characters, relations) -> ReviewSnapshot:
    people = tuple(sorted((c["id"], c["name"], c["description"]) for c in characters))
    links = tuple(
        sorted(
            (
                r["source_kind"],
                r["source_character_id"] or 0,
                r["target_kind"],
                r["target_character_id"] or 0,
                r["label"],
            )
            for r in relations
            if r["label"] is not None and r["label"].strip()
        )
    )
    return ReviewSnapshot(world_description, people, links)


# 통과 지문 개정. 검토 비교 내용의 구성·인코딩이나 평가 계약이 바뀌면 올린다(예전 지문은 재사용되지
# 않는다). DB 형식 검사(stories_rating_pass_digest_check)는 숫자 개정이면 그대로 받는다.
PASS_DIGEST_REVISION = 1
# 평가기 이름공간. 공급자·모델·정책 개정을 담는 짧은 식별자이며 원문을 담지 않는다.
_NAMESPACE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")


def valid_namespace(namespace: str | None) -> str | None:
    if namespace is not None and (
        not isinstance(namespace, str) or not _NAMESPACE.fullmatch(namespace)
    ):
        raise ValueError("reuse_namespace는 소문자·숫자·._- 1~64자여야 합니다.")
    return namespace


def content_digest(story_id: int, snapshot: ReviewSnapshot) -> str:
    """검토 비교 내용의 결정적 해시(16진수). 이름공간과 무관하다(같은 내용 비교용).

    스토리 ID·개정과 필드 이름·순서를 함께 넣어 구조가 다른 내용이 같은 해시가 되지 않게 한다.
    """
    payload = {
        "revision": PASS_DIGEST_REVISION,
        "story_id": int(story_id),
        "world_description": snapshot.world_description,
        "characters": [list(person) for person in snapshot.characters],
        "relations": [list(link) for link in snapshot.relations],
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def pass_digest(namespace: str, story_id: int, snapshot: ReviewSnapshot) -> str:
    """DB에 두는 통과 지문. `v<개정>:<이름공간>:<내용 해시>`."""
    return f"v{PASS_DIGEST_REVISION}:{namespace}:{content_digest(story_id, snapshot)}"


def same_content(stored: str | None, story_id: int, snapshot: ReviewSnapshot) -> bool:
    """저장된 지문이 이 내용(현재 개정)을 가리키는가. 이름공간은 보지 않는다."""
    if not stored:
        return False
    revision, _, rest = stored.partition(":")
    return revision == f"v{PASS_DIGEST_REVISION}" and rest.rpartition(":")[2] == content_digest(
        story_id, snapshot
    )


def capture_snapshot(connection, story_id) -> ReviewSnapshot:
    """같은 트랜잭션 안에서 이번 저장이 쓴 정본을 읽는다."""
    return build_snapshot(
        repository.read_basic(connection, story_id)["world_description"],
        repository.read_characters(connection, story_id),
        initial_state.read_relations(connection, story_id),
    )


class ReviewEvaluator(Protocol):
    def evaluate(self, story_id: int, snapshot: ReviewSnapshot) -> EligibilityVerdict:
        """검증된 적격 판정만 통과다(`is_eligible`). 그 밖의 값·예외는 통과가 아니다.

        조정기 잠금 밖에서 부른다. CREATE-US-08-AC09: 평가기가 선택적으로
        `evaluate_attributed(story_id, snapshot) -> AttributedVerdict`도 주면 조정기는 그것을
        대신 부른다(판정은 같고 사유 대상만 더해진다).
        """


class LastPassStore(Protocol):
    """마지막 통과 기록. 조정기 잠금 안에서 부르므로 막히지 않는 프로세스 안 구현만 허용한다.

    네트워크·DB 등 외부 IO가 필요한 영속 보관소로 바꿀 때는 이 프로토콜을 그대로 쓰지 말고
    잠금 밖에서 읽고 쓰도록 조정기 구조를 함께 바꾼다.
    """

    def get(self, story_id: int) -> ReviewSnapshot | None: ...

    def set(self, story_id: int, snapshot: ReviewSnapshot) -> None: ...


class ProcessLastPassStore:
    """프로세스 메모리 통과 기록(비영속). 조정기 잠금이 동기화를 맡는다."""

    def __init__(self):
        self.passes: dict[int, ReviewSnapshot] = {}

    def get(self, story_id):
        return self.passes.get(story_id)

    def set(self, story_id, snapshot):
        self.passes[story_id] = snapshot


@dataclass(frozen=True)
class ReviewResult:
    """DB에 쓸 검토 결과. `review_result`·`FAILURE`만 만든다(허용 목록 값)."""

    effective_rating: str | None  # None이면 앞선 적용 등급을 유지한다.
    status: str
    reason: str | None
    # 검증된 적격 판정이었는가. 통과 지문은 이것이 참일 때만 쓴다.
    passed: bool = False
    # CREATE-US-08-AC09 사유 대상. 판정 사유·검토 비교 내용에 묶인 것만 남는다(판정과 무관).
    targets: frozenset = frozenset()


def next_pass_digest(stored, namespace, story_id, snapshot, result: ReviewResult) -> str | None:
    """결과를 쓸 때 함께 둘 통과 지문. 스토리 잠금 안에서 DB에서 읽은 `stored`로 정한다.

    - 적격 완료: 이 내용의 지문(이름공간이 없으면 비움).
    - 부적격·판단 불가 완료: 같은 내용의 지문이면 비우고, 다른 내용의 예전 통과는 그대로 둔다.
    - 실패: 그대로 둔다.
    """
    if result.passed:
        return None if namespace is None else pass_digest(namespace, story_id, snapshot)
    if result.status == "completed" and same_content(stored, story_id, snapshot):
        return None
    return stored


_OUTCOME_TARGETS = {
    Eligibility.ELIGIBLE: ("UNSAFE", "completed"),
    Eligibility.INELIGIBLE: ("SAFE", "completed"),
    Eligibility.UNDETERMINED: ("SAFE", "completed"),
    Eligibility.UNAVAILABLE: (None, "failed"),
}


def encode_reason(verdict: EligibilityVerdict) -> str | None:
    """검증된 판정을 `<판정>:<사유>,<사유>` 코드로 바꾼다. 적격은 사유가 없다(None)."""
    if verdict.outcome is Eligibility.ELIGIBLE:
        return None
    return f"{verdict.outcome.value}:{','.join(sorted(r.value for r in verdict.reasons))}"


def decode_reason(text) -> EligibilityVerdict | None:
    """저장된 사유 코드를 허용 목록으로 다시 검증한다. 맞지 않으면 None(그대로 내보내지 않는다)."""
    if not isinstance(text, str):
        return None
    outcome, sep, codes = text.partition(":")
    try:
        if not sep or outcome == Eligibility.ELIGIBLE:
            return None
        return EligibilityVerdict(
            outcome=Eligibility(outcome),
            reasons=frozenset(EligibilityReason(code) for code in codes.split(",")),
        )
    except ValueError:
        return None


def reason_payload(text) -> dict | None:
    """응답 `rating_review_reason` 값. 허용 목록으로 다시 검증한 코드만, 아니면 None."""
    verdict = decode_reason(text)
    if verdict is None:
        return None
    return {"outcome": verdict.outcome.value, "reasons": sorted(r.value for r in verdict.reasons)}


_TARGET_CONTAINERS = (frozenset, set, tuple, list)


def _checked_targets(fields: dict) -> frozenset:
    """대상 붙은 판정의 대상을 하나씩 엄격히 다시 검증한다. 잘못된 대상·묶음은 뺀다."""
    if fields.keys() - AttributedVerdict.model_fields.keys():
        return frozenset()  # 계약 밖 필드(평가기 원문 등)가 섞인 대상 묶음은 믿지 않는다.
    targets = fields.get("targets")
    if type(targets) not in _TARGET_CONTAINERS:
        return frozenset()
    kept = set()
    for item in targets:
        checked = revalidated(ReviewTarget, item)
        if checked is not None:
            kept.add(checked)
    return frozenset(kept)


def split_verdict(value) -> tuple[EligibilityVerdict | None, frozenset]:
    """평가기 반환값(판정 또는 대상 붙은 판정)을 엄격히 다시 검증해 (판정, 대상)으로 나눈다.

    판정과 대상은 따로 검증한다. 판정이 잘못되면 (None, 빈 대상)이다. 대상 메타데이터가
    잘못돼도 독립적으로 유효한 판정(강등 등)은 그대로 두고 잘못된 대상만 뺀다(CREATE-US-08-AC09).
    대상은 판정에 쓰이지 않는다.
    """
    if isinstance(value, AttributedVerdict):
        fields = vars(value)
        checked = revalidated(EligibilityVerdict, fields.get("verdict"))
        if checked is None:
            return None, frozenset()
        return checked, _checked_targets(fields)
    return revalidated(EligibilityVerdict, value), frozenset()


def tied_targets(verdict: EligibilityVerdict, targets, snapshot) -> frozenset:
    """판정 사유와 검토 비교 내용(명단 인물)에 묶이는 대상만 남긴다. 묶을 수 없으면 뺀다."""
    if snapshot is None or verdict.outcome not in (
        Eligibility.INELIGIBLE,
        Eligibility.UNDETERMINED,
    ):
        return frozenset()
    roster = {person[0] for person in snapshot.characters}
    return frozenset(
        t
        for t in targets
        if t.reason in verdict.reasons and (t.character_id is None or t.character_id in roster)
    )


# --- CREATE-US-08-AC09 사유 대상 저장·조회 --------------------------------------------------------
# 저장(stories.rating_review_targets, jsonb): 대상마다 사유 코드·종류·명단 인물 DB ID(유저 캐릭터는
# null)만 두고 이름·원문은 두지 않는다. 결과를 쓴 요청 표·사유 코드·정본 내용 해시를 함께 둬, 조회할
# 때 그 셋이 모두 지금 값과 같을 때만 대상을 낸다(대상을 모르는 이전 버전 작성기가 남긴 오래된
# 대상은 사유만 남기고 뺀다). 예전 사유 컬럼(rating_review_reason)의 형식은 바꾸지 않는다.
# 응답: 사유 코드·종류·표시 라벨(제작자가 쓴 인물 이름 또는 고정 역할 문구)만 싣는다.
TARGETS_REVISION = 1
MAX_TARGETS_BYTES = 16384  # DB 제약(stories_rating_review_targets_check)과 같다.
USER_ROLE_LABEL = "유저"  # 유저 캐릭터(유저 슬롯) 고정 역할 문구
_TARGETS_KEYS = frozenset({"v", "ticket", "reason", "content", "targets"})
_TARGET_KEYS = frozenset({"reason", "kind", "character_id"})


def encode_targets(story_id, snapshot: ReviewSnapshot, ticket: datetime, result) -> str | None:
    """결과와 함께 쓸 대상 JSON. 대상이 없거나 크기를 넘으면 None(사유만 남는다)."""
    if not result.targets or result.reason is None:
        return None
    items = sorted(
        result.targets,
        key=lambda t: (
            t.reason.value,
            t.kind.value,
            -1 if t.character_id is None else t.character_id,
        ),
    )
    document = {
        "v": TARGETS_REVISION,
        "ticket": ticket.isoformat(),
        "reason": result.reason,
        "content": content_digest(story_id, snapshot),
        "targets": [
            {"reason": t.reason.value, "kind": t.kind.value, "character_id": t.character_id}
            for t in items
        ],
    }
    text = json.dumps(document, separators=(",", ":"))
    return text if len(text.encode()) <= MAX_TARGETS_BYTES else None


def _bound_document(row):
    """저장된 대상이 지금의 사유·요청 표에 묶이면 그 문서, 아니면 None."""
    document = row.get("rating_review_targets")
    ticket = row.get("rating_requested_at")
    if (
        type(document) is not dict
        or document.keys() != _TARGETS_KEYS
        or type(document["v"]) is not int
        or document["v"] != TARGETS_REVISION
        or type(document["reason"]) is not str
        or document["reason"] != row["rating_review_reason"]
        or type(document["ticket"]) is not str
        or type(document["content"]) is not str
        or type(document["targets"]) is not list
        or ticket is None
    ):
        return None
    try:
        if datetime.fromisoformat(document["ticket"]) != ticket:
            return None
    except (ValueError, TypeError):
        return None
    return document


_LINE_BREAKS = re.compile(r"[ \t\n\r]*[\t\n\r][ \t\n\r]*")
# 앱 readTarget이 거부하는 제어 문자(C0·DEL·C1), 줄·문단 구분자, 양방향 제어.
_UNSAFE_LABEL_CHARS = re.compile("[\x00-\x1f\x7f-\x9f  ؜‎‏‪-‮⁦-⁩]")


def display_name(name) -> str | None:
    """명단 이름의 한 줄 라벨. 탭·LF·CR은 공백 하나로, 다른 제어 문자가 있으면 None."""
    if type(name) is not str or "﻿" in name:
        # FEFF는 앱 JS trim은 지우고 Python strip은 남겨 양쪽 라벨이 갈리므로 정규화 전에 거부한다.
        return None
    label = _LINE_BREAKS.sub(" ", name).strip()
    if not label or _UNSAFE_LABEL_CHARS.search(label):
        return None
    return label


def target_label(target: ReviewTarget, names: dict) -> str | None:
    """표시 라벨. 명단에 없거나 유저·다른 인물과 구별되지 않으면 None(뺀다).

    명단 원본 이름(저장값·해시 입력)은 바꾸지 않고 응답 라벨만 정규화한다.
    """
    if target.character_id is None:
        return USER_ROLE_LABEL  # 계약상 인물 종류만 유저 캐릭터일 수 있다.
    if target.character_id not in names:
        return None
    label = display_name(names[target.character_id])
    if label is None or label == USER_ROLE_LABEL:
        # 「유저」라는 명단 인물은 유저 캐릭터와 구별되지 않는다(관계 「유저 ↔ 유저」 포함).
        return None
    if any(
        other_id != target.character_id and display_name(other) == label
        for other_id, other in names.items()
    ):
        return None  # 정규화 뒤 같은 라벨인 다른 인물과 구별되지 않는다.
    if target.kind is TargetKind.RELATIONSHIP:
        return f"{USER_ROLE_LABEL} ↔ {label}"  # 출발·방향을 정하지 않은 쌍
    return label


def review_targets(connection, story_id, row) -> list[dict] | None:
    """응답 대상 목록. 사유·요청 표·정본 내용에 묶이지 않으면 None, 묶이지 않는 항목은 뺀다."""
    verdict = decode_reason(row["rating_review_reason"])
    if verdict is None or row["rating_review_status"] != "completed":
        return None
    document = _bound_document(row)
    if document is None:
        return None
    snapshot = capture_snapshot(connection, story_id)
    if document["content"] != content_digest(story_id, snapshot):
        return None
    names = {person[0]: person[1] for person in snapshot.characters}
    found = set()
    for item in document["targets"]:
        if type(item) is not dict or item.keys() != _TARGET_KEYS:
            continue
        try:
            target = ReviewTarget.model_validate(item)
        except (ValidationError, ValueError, TypeError):
            continue
        if target.reason not in verdict.reasons:
            continue
        label = target_label(target, names)
        if label is not None:
            found.add((target.reason.value, target.kind.value, label))
    return [{"reason": r, "kind": k, "label": label} for r, k, label in sorted(found)] or None


def reason_view(connection, story_id, row) -> dict | None:
    """응답 `rating_review_reason`. 묶이는 대상이 있을 때만 `targets`를 더한다."""
    payload = reason_payload(row["rating_review_reason"])
    if payload is not None:
        targets = review_targets(connection, story_id, row)
        if targets:
            payload["targets"] = targets
    return payload


def review_result(verdict, snapshot: ReviewSnapshot | None = None) -> ReviewResult:
    """평가기 반환값을 엄격히 다시 검증해 결과로 바꾼다. 잘못된 값은 실패(`invalid_signals`)다.

    CREATE-US-08-AC09: 대상 붙은 판정이면 `snapshot`에 묶이는 대상만 결과에 싣는다.
    """
    checked, targets = split_verdict(verdict)
    if checked is None:
        checked = unavailable(EligibilityReason.INVALID_SIGNALS)
    effective, status = _OUTCOME_TARGETS[checked.outcome]
    return ReviewResult(
        effective,
        status,
        encode_reason(checked),
        checked.outcome is Eligibility.ELIGIBLE,
        tied_targets(checked, targets, snapshot),
    )


# 평가기 예외·넘김 실패·울타리 대기 시간 초과. 앞선 적용 등급을 유지한다.
FAILURE = ReviewResult(None, "failed", encode_reason(unavailable()))


class ReviewResults(Protocol):
    """검토 결과 보관소. 조정기 잠금 밖(작업자 또는 커밋 뒤 요청 스레드)에서 부른다."""

    def apply(
        self,
        story_id: int,
        snapshot: ReviewSnapshot,
        ticket: datetime,
        result: ReviewResult,
        namespace: str | None = None,
    ) -> bool:
        """요청 표와 정본 내용이 그대로일 때만 쓰고, 썼는지 돌려준다.

        현재 검토 상태(stories)와 통과 지문만 쓴다(`next_pass_digest`). 요청키 영수증(재전송
        응답)은 커밋 시점 값으로 고정되어 바꾸지 않는다.
        """


def _positive(name: str, value: float | None) -> float | None:
    if value is not None and not value > 0:
        raise ValueError(f"{name}는 0보다 커야 합니다.")
    return value


class DatabaseReviewResults:
    """요청과 독립된 새 DB 연결로 결과를 쓴다. 접속 정보는 주입받는다(환경변수를 읽지 않는다).

    시간 제한(초)은 주었을 때만 이 결과 쓰기 연결에만 건다(기본 None은 기존처럼 제한 없음).
    - `connect_timeout`: 이 연결의 접속 대기(libpq, 정수 초로 올림·최소 2초).
    - `lock_timeout`·`statement_timeout`: 결과 쓰기 트랜잭션 안에서만 `set_config(..., true)`로
      건다(트랜잭션이 끝나면 사라지며 DB·역할·풀 설정은 바꾸지 않는다).
    시간 초과는 예외로 끝나 결과를 쓰지 않는다(조정기가 로그만 남기고, 그 스토리는 다음 확정
    저장까지 `reviewing`으로 남는다). 결과 한 건의 상한일 뿐 접속 뒤 네트워크 정지 등 모든 경우의
    절대 상한은 아니다.
    """

    def __init__(
        self,
        conninfo: str,
        *,
        connect_timeout: float | None = None,
        lock_timeout: float | None = None,
        statement_timeout: float | None = None,
    ):
        self._conninfo = conninfo
        self._connect_timeout = _positive("connect_timeout", connect_timeout)
        self._lock_timeout = _positive("lock_timeout", lock_timeout)
        self._statement_timeout = _positive("statement_timeout", statement_timeout)

    def __repr__(self):
        return "DatabaseReviewResults(...)"  # 접속 정보를 싣지 않는다.

    def _connect(self):
        options = {}
        if self._connect_timeout is not None:
            options["connect_timeout"] = max(2, math.ceil(self._connect_timeout))
        return psycopg.connect(self._conninfo, autocommit=True, row_factory=dict_row, **options)

    def _bound_transaction(self, connection) -> None:
        """결과 쓰기 트랜잭션 안에서만 유효한 잠금·문장 시간 제한(밀리초)."""
        for name, seconds in (
            ("lock_timeout", self._lock_timeout),
            ("statement_timeout", self._statement_timeout),
        ):
            if seconds is not None:
                connection.execute(
                    "SELECT set_config(%s, %s, true)", (name, f"{math.ceil(seconds * 1000)}ms")
                )

    def apply(self, story_id, snapshot, ticket, result, namespace=None) -> bool:
        with self._connect() as connection:
            with connection.transaction():
                self._bound_transaction(connection)
                row = repository.lock_review_target(connection, story_id)
                if (
                    row is None
                    or row["deleted_at"] is not None
                    or row["requested_rating"] != "UNSAFE"
                    or row["rating_review_status"] != "reviewing"
                    or row["rating_requested_at"] != ticket
                ):
                    return False
                # 지문은 잠근 뒤 다시 읽은 정본 내용으로 만든다(평가한 내용과 같을 때만).
                canonical = capture_snapshot(connection, story_id)
                if canonical != snapshot:
                    return False
                return repository.apply_review_result(
                    connection,
                    story_id,
                    ticket,
                    result.effective_rating,
                    result.status,
                    result.reason,
                    next_pass_digest(
                        row["rating_pass_digest"], namespace, story_id, canonical, result
                    ),
                    # CREATE-US-08-AC09: 같은 잠금·같은 UPDATE에서 정본 내용으로 만든 대상
                    encode_targets(story_id, canonical, ticket, result),
                )


@dataclass
class Publication:
    """울타리 한 번의 결과. `outcome`은 울타리를 나온 뒤 채워진다."""

    generation: int
    outcome: str | None = None


class RatingReviewCoordinator:
    """프로세스 안 백그라운드 검토 조정기. 상태는 메모리에만 있어 재시작하면 사라진다."""

    def __init__(
        self,
        evaluator: ReviewEvaluator,
        store: LastPassStore | None = None,
        max_workers: int = 1,
        fence_timeout: float = 30.0,
        results: ReviewResults | None = None,
        reuse_namespace: str | None = None,
    ):
        self._evaluator = evaluator
        self._results = results
        # 영속 통과 재사용 이름공간(결과 보관소가 있을 때만 쓴다). None이면 재사용·지문 기록이 없다.
        self._reuse_namespace = valid_namespace(reuse_namespace)
        self._store = store if store is not None else ProcessLastPassStore()
        self._executor = ThreadPoolExecutor(max_workers, thread_name_prefix="rating-review")
        self._fence_timeout = fence_timeout
        # 아래 상태는 모두 이 조건 변수(RLock)로 보호한다.
        self._cond = threading.Condition()
        self._generations = itertools.count(1)
        # 스토리별 마지막으로 알린 (세대, 내용). 내용 None은 검토 대상이 아님(SAFE·읽기 실패).
        self._latest: dict[int, tuple[int, ReviewSnapshot | None]] = {}
        # 스토리별 열린 울타리 수(커밋 중인 확정 저장)
        self._fenced: dict[int, int] = {}
        # 스토리별 맡은(대기·평가 중) 내용 → 맡긴 세대
        self._active: dict[int, dict[ReviewSnapshot, int]] = {}
        self._futures: set[Future] = set()

    @property
    def persists(self) -> bool:
        """검토 결과를 DB에 쓰는가(결과 보관소를 줬는가)."""
        return self._results is not None

    @property
    def reuse_namespace(self) -> str | None:
        """영속 마지막 통과를 재사용할 이름공간. 결과 보관소가 없으면 None."""
        return self._reuse_namespace if self.persists else None

    @contextmanager
    def fence(
        self,
        story_id: int,
        snapshot: ReviewSnapshot | None,
        request_ticket: datetime | None = None,
        reused: bool = False,
    ):
        """확정 저장 트랜잭션의 마지막 단계(스토리 잠금 안, COMMIT 직전)에 들어간다.

        트랜잭션을 감싼 바깥 문맥에서 나오므로 COMMIT이 끝난 뒤 나온다. 예외 없이 나올 때만
        세대·내용을 알리고, 예외(커밋·저장 실패)면 알리지 않는다. 알림 실패는 로그만 남긴다.
        `request_ticket`은 결과 저장 경로에서 이번 저장이 쓴 요청 표다. `reused`면 이번 저장이
        영속 통과를 재사용해 완료로 썼으므로 넘기지 않고, 최신 내용을 검토 대상 아님으로 알려 이
        프로세스의 대기·평가 중 검토가 결과·통과를 남기지 않게 한다.
        """
        with self._cond:
            ticket = Publication(next(self._generations))
            self._fenced[story_id] = self._fenced.get(story_id, 0) + 1
        committed = False
        try:
            yield ticket
            committed = True
        finally:
            with self._cond:
                try:
                    if committed:
                        ticket.outcome = self._publish(
                            story_id, ticket.generation, snapshot, request_ticket, reused
                        )
                except Exception as exc:
                    ticket.outcome = "failed"
                    logger.warning(
                        "등급 검토 알림 실패 story_id=%s generation=%s error=%s",
                        story_id,
                        ticket.generation,
                        type(exc).__name__,
                    )
                finally:
                    remaining = self._fenced[story_id] - 1
                    if remaining:
                        self._fenced[story_id] = remaining
                    else:
                        del self._fenced[story_id]
                    self._cond.notify_all()

    def publish(self, story_id: int, snapshot: ReviewSnapshot | None) -> str:
        """DB 트랜잭션 없이 바로 알린다(울타리에 들어갔다 곧바로 나온다)."""
        with self.fence(story_id, snapshot) as ticket:
            pass
        return ticket.outcome

    def _publish(self, story_id, generation, snapshot, request_ticket=None, reused=False) -> str:
        """조정기 잠금 안에서 부른다. 외부 IO 없이 끝난다."""
        latest = self._latest.get(story_id)
        if latest is not None and generation <= latest[0]:
            return "stale"  # 더 나중에 커밋된 저장이 이미 알려졌다.
        if reused:
            # 이미 완료로 커밋됐다. 대기·평가 중인 검토는 모두 최신이 아니게 된다.
            self._latest[story_id] = (generation, None)
            return "reused"
        self._latest[story_id] = (generation, snapshot)
        if snapshot is None:
            return "invalidated"
        if request_ticket is not None:
            # 결과 저장 경로는 메모리 통과 기록·중복 제거를 쓰지 않고 요청 표마다 평가한다.
            return self._submit(story_id, snapshot, generation, request_ticket)
        try:
            passed = self._store.get(story_id) == snapshot
        except Exception as exc:
            # 통과 여부를 모르면 통과로 보지 않고 다시 검토한다.
            logger.warning(
                "등급 검토 통과 기록 읽기 실패 story_id=%s error=%s", story_id, type(exc).__name__
            )
            passed = False
        if passed:
            return "already_passed"
        active = self._active.setdefault(story_id, {})
        if snapshot in active:
            return "deduplicated"
        try:
            future = self._executor.submit(self._run, story_id, snapshot, generation)
        except Exception as exc:
            if not active:
                del self._active[story_id]
            logger.warning(
                "등급 검토 넘김 실패 story_id=%s generation=%s error=%s",
                story_id,
                generation,
                type(exc).__name__,
            )
            return "failed"
        # 작업자는 첫 단계에서 이 잠금을 기다리므로 아래 기록보다 먼저 진행하지 않는다.
        active[snapshot] = generation
        self._futures.add(future)
        # RLock이라 이미 끝난 future의 콜백이 여기서 바로 불려도 막히지 않는다.
        future.add_done_callback(partial(self._finished, story_id, snapshot, generation))
        return "submitted"

    def _submit(self, story_id, snapshot, generation, request_ticket) -> str:
        """결과 저장 경로의 넘김. 조정기 잠금 안에서 부른다."""
        try:
            future = self._executor.submit(
                self._run, story_id, snapshot, generation, request_ticket
            )
        except Exception as exc:
            logger.warning(
                "등급 검토 넘김 실패 story_id=%s generation=%s error=%s",
                story_id,
                generation,
                type(exc).__name__,
            )
            return "failed"
        self._futures.add(future)
        future.add_done_callback(partial(self._finished, story_id, snapshot, generation))
        return "submitted"

    def _record(self, story_id, snapshot, generation, request_ticket, result) -> bool:
        """조정기 잠금 밖에서 부른다. 요청 표·정본 내용이 그대로일 때만 결과가 쓰인다."""
        try:
            applied = self._results.apply(
                story_id, snapshot, request_ticket, result, namespace=self._reuse_namespace
            )
        except Exception as exc:
            logger.warning(
                "등급 검토 결과 기록 실패 story_id=%s generation=%s error=%s",
                story_id,
                generation,
                type(exc).__name__,
            )
            return False
        if not applied:
            logger.info("등급 검토 결과 버림 story_id=%s generation=%s", story_id, generation)
        return applied

    def record_handoff(self, story_id, snapshot, request_ticket, publication) -> None:
        """커밋 뒤, 울타리를 나온 뒤 요청 스레드에서 부른다. 넘기지 못했으면 실패로 기록한다.

        커밋되지 않았으면(`outcome` 없음) 아무것도 하지 않는다. 더 나중 저장이 먼저 알려졌으면
        요청 표가 달라 쓰이지 않는다. 현재 검토 상태만 쓰고, 요청키 영수증과 이미 만든 응답은
        커밋 시점 값(검토 중)으로 두며 바꾸지 않는다(CREATE-US-08-AC02·AC11).
        """
        if publication is None or publication.outcome in (None, "submitted", "reused"):
            return
        self._record(story_id, snapshot, publication.generation, request_ticket, FAILURE)

    def _finished(self, story_id, snapshot, generation, future):
        with self._cond:
            self._futures.discard(future)
            if future.cancelled():
                self._release(story_id, snapshot, generation)
            self._cond.notify_all()

    def _release(self, story_id, snapshot, generation):
        active = self._active.get(story_id)
        if active is not None and active.get(snapshot) == generation:
            del active[snapshot]
            if not active:
                del self._active[story_id]

    def _is_current(self, story_id, snapshot, generation=None) -> bool:
        """잠금 안에서 부른다. 커밋 중인 울타리가 닫히기를 기다린 뒤 최신 내용인지 본다.

        `generation`을 주면(결과 저장 경로) 내용뿐 아니라 맡긴 세대도 최신 세대와 정확히 같아야
        한다. 대기 중에 같은 내용이 다시 알려졌으면(예: B → 재사용 A → B) 앞선 세대는 평가하지
        않는다(요청 표가 달라 어차피 쓰이지 않는다). 주지 않으면 기존 프로세스 경로의 내용 기준
        중복 제거 의미를 그대로 둔다.
        """
        settled = self._cond.wait_for(
            lambda: not self._fenced.get(story_id), timeout=self._fence_timeout
        )
        if not settled:
            logger.warning("등급 검토 울타리 대기 시간 초과 story_id=%s", story_id)
            return False
        latest = self._latest.get(story_id)
        if latest is None or latest[1] != snapshot:
            return False
        return generation is None or latest[0] == generation

    def _run(self, story_id, snapshot, generation, request_ticket=None):
        released = False
        try:
            with self._cond:
                # 결과 저장 경로는 맡긴 세대까지 정확히 최신이어야 평가한다.
                exact = generation if request_ticket is not None else None
                current = self._is_current(story_id, snapshot, exact)
                if not current:
                    # 대기 중에 더 나중 저장이 커밋됐다(또는 울타리 대기 시간 초과).
                    # 평가하지 않는다.
                    self._release(story_id, snapshot, generation)
                    released = True
            if not current:
                if request_ticket is not None:
                    # 이 요청 표가 아직 최신이면(시간 초과) 검토 중으로 남기지 않고 실패로 둔다.
                    self._record(story_id, snapshot, generation, request_ticket, FAILURE)
                return
            outcome = FAILURE
            try:
                # CREATE-US-08-AC09: 대상을 함께 주는 평가기면 그것을 쓴다(판정은 같다).
                attribute = getattr(self._evaluator, "evaluate_attributed", None)
                if attribute is not None:
                    result = attribute(story_id, snapshot)
                else:
                    result = self._evaluator.evaluate(story_id, snapshot)
                outcome = review_result(result, snapshot)
                checked = split_verdict(result)[0]
                passed = checked is not None and checked.outcome is Eligibility.ELIGIBLE
                if not passed:
                    # 검증된 판정의 종류·사유 코드만 남긴다. 잘못된 값은 필드·타입 이름도 읽지 않고
                    # 고정 코드만 남긴다(원문·평가기 출력이 섞일 수 있다).
                    logger.info(
                        "등급 검토 미통과 story_id=%s generation=%s outcome=%s reasons=%s",
                        story_id,
                        generation,
                        checked.outcome.value if checked is not None else "invalid",
                        sorted(r.value for r in checked.reasons) if checked is not None else [],
                    )
            except Exception as exc:
                logger.warning(
                    "등급 검토 실패 story_id=%s generation=%s error=%s",
                    story_id,
                    generation,
                    type(exc).__name__,
                )
                passed = False
                outcome = FAILURE
            if request_ticket is not None:
                # 조정기 잠금 밖에서 독립 연결로 쓴다.
                self._record(story_id, snapshot, generation, request_ticket, outcome)
            with self._cond:
                # 판정 기록과 맡은 내용 해제를 한 번에 한다.
                try:
                    # 실패·미통과는 예전 통과를 그대로 둔다. 다른 내용이 이미 커밋됐으면 버린다.
                    if passed and self._is_current(story_id, snapshot):
                        self._store.set(story_id, snapshot)
                except Exception as exc:
                    logger.warning(
                        "등급 검토 통과 기록 실패 story_id=%s generation=%s error=%s",
                        story_id,
                        generation,
                        type(exc).__name__,
                    )
                finally:
                    self._release(story_id, snapshot, generation)
                    released = True
        finally:
            if not released:
                with self._cond:
                    self._release(story_id, snapshot, generation)

    def wait(self, timeout: float | None = None) -> bool:
        """지금까지 넘긴 검토(기다리는 중 넘긴 것 포함)가 끝날 때까지 기다린다(테스트·종료용)."""
        with self._cond:
            return self._cond.wait_for(lambda: not self._futures, timeout=timeout)

    def close(self) -> None:
        """대기 중인 검토는 취소하고, 평가 중인 검토는 기다리지 않는다.

        주입한 쪽이 소유한 조정기·기존 호출자용 즉시 종료다(동작 그대로).
        """
        self._executor.shutdown(wait=False, cancel_futures=True)

    def drain(self) -> None:
        """새 검토를 받지 않고, 대기열의 검토와 평가 중인 검토가 모두 끝날 때까지 기다린다.

        앱이 소유한 조정기의 정상 종료용이다. 대기열 검토를 취소하지 않는다. 막히는 호출이므로
        이벤트 루프에서는 별도 스레드로 부른다. 전체 대기 시간에는 상한이 없다(검토 하나는 평가기
        마감·울타리 대기·결과 DB 시간 제한으로 끝나지만, 대기열 길이만큼 늘어난다). 종료 뒤 넘김은
        실패(`failed`)로 돌아온다.
        """
        self._executor.shutdown(wait=True, cancel_futures=False)


def capture(connection, story_id) -> ReviewSnapshot | None:
    """확정 저장 트랜잭션 안(스토리 잠금 중), 모든 쓰기를 마친 뒤 부른다.

    저장본 등급 선택이 UNSAFE면 검토 비교 내용을, 아니면 None을 돌려준다. 읽기 오류는
    세이브포인트로 되돌리고 None을 돌려줘 저장 자체는 실패시키지 않는다(None도 앞선 검토를
    무효로 한다).
    """
    try:
        with connection.transaction():
            if repository.read_rating(connection, story_id)["requested_rating"] != "UNSAFE":
                return None
            return capture_snapshot(connection, story_id)
    except Exception as exc:
        logger.warning(
            "등급 검토 내용 수집 실패 story_id=%s error=%s", story_id, type(exc).__name__
        )
        return None


@dataclass(frozen=True)
class FinalSaveHandoff:
    snapshot: ReviewSnapshot | None = field(repr=False)
    request_ticket: datetime | None = None
    # 영속 마지막 통과를 재사용해 이미 완료로 썼다(넘기지 않는다).
    reused: bool = False


def prepare_final_save(
    connection, story_id, coordinator: RatingReviewCoordinator
) -> FinalSaveHandoff:
    """확정 저장 트랜잭션 안(스토리 잠금 중), 모든 쓰기 뒤·응답을 만들기 전에 부른다.

    결과 저장 경로면 UNSAFE는 검토 중·새 요청 표를, SAFE는 적용 등급 SAFE를 같은 트랜잭션에
    쓴다. UNSAFE인데 내용을 읽지 못했으면 넘길 수 없으므로 실패로 쓴다(적용 등급 유지).
    CREATE-US-08-AC14: 방금 쓴 정본 내용의 지문이 DB의 마지막 통과 지문과 정확히 같으면
    평가 없이 완료(UNSAFE)·새 요청 표를 쓴다(새 요청 표가 앞선 대기 검토를 막는다).
    """
    snapshot = capture(connection, story_id)
    if not coordinator.persists:
        return FinalSaveHandoff(snapshot)
    if snapshot is not None:
        namespace = coordinator.reuse_namespace
        if namespace is not None:
            ticket = repository.reuse_review_pass(
                connection, story_id, pass_digest(namespace, story_id, snapshot)
            )
            if ticket is not None:
                logger.info("등급 검토 통과 재사용 story_id=%s", story_id)
                return FinalSaveHandoff(snapshot, ticket, reused=True)
        return FinalSaveHandoff(snapshot, repository.mark_review_pending(connection, story_id))
    if repository.read_rating(connection, story_id)["requested_rating"] == "UNSAFE":
        repository.mark_review_failed(connection, story_id, FAILURE.reason)
    else:
        repository.mark_rating_safe(connection, story_id)
    return FinalSaveHandoff(None)


def fence_final_save(
    story_id,
    coordinator: RatingReviewCoordinator,
    after_commit,
    handoff: FinalSaveHandoff,
):
    """확정 저장 트랜잭션의 마지막 단계로 울타리에 들어간다. 이 뒤로는 COMMIT 말고 SQL이 없다.

    `after_commit`은 트랜잭션을 감싼 `ExitStack`이다. 울타리는 커밋이 끝난 뒤 `after_commit`이
    닫힐 때 나온다. 결과 저장 경로면 그 뒤(조정기 잠금 밖) 넘기지 못한 요청을 현재 검토 상태에
    실패로 기록한다(같은 요청 표일 때만). 요청키 영수증과 이번 응답은 커밋 시점 값 그대로다.
    """
    entered = {}
    if handoff.request_ticket is not None and not handoff.reused:

        def record():
            coordinator.record_handoff(
                story_id, handoff.snapshot, handoff.request_ticket, entered.get("publication")
            )

        # ExitStack은 거꾸로 닫히므로 울타리를 나온 뒤에 불린다.
        after_commit.callback(record)
    entered["publication"] = after_commit.enter_context(
        coordinator.fence(story_id, handoff.snapshot, handoff.request_ticket, handoff.reused)
    )
