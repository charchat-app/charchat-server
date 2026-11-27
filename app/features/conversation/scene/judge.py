"""CHAT-US-08 정상 완료된 턴의 장면 변화를 AI에게 묻는다.

응답을 만든 뒤, 같은 모델에 따로 한 번 더 물어 바뀐 항목만 JSON으로 받는다(KAN-40 §5.2·5.3).
이 판단 호출은 이용자에게 청구하지 않는다(설계 추천안, 나래님 확인 전). 실패하거나 읽을 수 없으면
장면을 바꾸지 않고 응답 저장은 그대로 진행한다.
"""

import logging
from dataclasses import replace
from time import monotonic

from app.core.config import settings
from app.features.conversation.messaging import providers
from app.features.conversation.messaging.prompt import PromptContext, label_parts
from app.features.conversation.scene.rules import ScenePatch, evidence_found, parse_judgement

logger = logging.getLogger(__name__)

DATE_MODE = {
    "calendar": "달력 날짜",
    "world": "세계관 날짜",
    "elapsed": "경과일(기준 사건 이후 N일째)",
}

JUDGE_SYSTEM = "\n".join(
    [
        (
            "너는 롤플레이 대화의 장면 기록 담당이다. 이번 턴(이용자 입력과 AI 응답)에서 "
            "실제로 일어난 장면 변화만 골라 JSON 하나로 답한다. 설명이나 다른 글은 쓰지 "
            "않는다."
        ),
        "",
        "판단 규칙",
        (
            "- 이번 턴에서 실제로 일어난 일로 서술된 변화만 기록한다. "
            '계획·제안·가정·질문·농담·회상·꿈·예시는 변화가 아니다. 예: "내일 공원 갈래?", '
            '"어제 공원에서 봤잖아"는 장소 변화가 아니다.'
        ),
        (
            "- 이용자가 [나레이터]로 쓴 서술은 가장 확실한 근거다. 이용자 입력과 AI 응답이 "
            "다르면 이용자 입력을 따른다."
        ),
        (
            "- 장소: 다른 곳으로 이동한 것이 분명할 때만 새 장소를 쓴다. 같은 장소 안의 작은 "
            "이동은 쓰지 않는다."
        ),
        (
            '- 시각: 시각이 분명히 바뀌었을 때만 쓴다(시각 언급, "한 시간 뒤", "다음 날 점심"'
            ' 등). 대화하며 몇 분 흐른 것은 쓰지 않는다. "점심"처럼 때만 알면 대략의 '
            "시(12시)를 쓰고 분은 null로 둔다."
        ),
        (
            "- 날짜: 날이 바뀐 것이 분명할 때만 쓴다. 지난 날 수는 days_passed로, 특정 날짜로"
            " 건너뛰었으면 date로 쓴다."
        ),
        (
            "- 기준 사건(경과일 방식일 때만): 새 사건이 분명히 시작됐을 때만 "
            'new_reference_event에 짧게 쓴다(예: "탈출"). 기존 사건이 끝났다는 것만으로는 '
            "쓰지 않는다."
        ),
        (
            "- 캐릭터: 아래 목록의 이름만 쓴다. 갈아입었으면 clothing에 지금 입은 옷 전체를, "
            "마음이 분명히 바뀌었으면 inner_thought에 지금의 속마음을 짧게 쓴다. 바뀌지 않은 "
            "캐릭터는 넣지 않는다."
        ),
        "- 이용자 캐릭터의 착장·속마음은 기록하지 않는다.",
        (
            "- 글자 수: 장소 30자, 기준 사건 10자, 착장 300자, 속마음 100자 이내로 줄여 쓴다. "
            "자정을 넘겼으면 days_passed도 함께 쓴다."
        ),
        "- evidence에는 이번 턴 이용자 입력이나 AI 응답에 있는 문장을 고치지 말고 그대로 복사한다.",
        (
            "- 미지정 항목은 이야기에서 분명히 정해졌을 때만 채운다. 바뀐 것이 없으면 "
            '{"changed": false}만 쓴다.'
        ),
        "",
        "형식(바뀌지 않은 항목은 null 또는 생략)",
        '{"changed": true,',
        ' "place": "새 장소" | null,',
        ' "date": {"year": 숫자|null, "month": 숫자, "day": 숫자} | null,',
        ' "days_passed": 숫자 | null,',
        ' "time": {"hour": 0~23, "minute": 0~59|null} | null,',
        ' "new_reference_event": "새 기준 사건" | null,',
        (
            ' "characters": [{"name": "이름", "clothing": "새 착장"|null, "inner_thought": '
            '"새 속마음"|null}],'
        ),
        ' "evidence": "근거가 된 문장을 짧게 그대로",',
        ' "evidence_from": "user" | "assistant"}',
    ]
)


def _value(value, suffix: str = "") -> str:
    return "미지정" if value is None or value == "" else f"{value}{suffix}"


def describe_scene(scene: dict | None, character_states: list[dict]) -> str:
    scene = scene or {}
    mode = scene.get("date_mode")
    lines = [f"날짜 방식: {DATE_MODE.get(mode, '없음(날짜를 다루지 않음)')}"]
    if mode in ("calendar", "world"):
        era = f"{scene['era']} " if mode == "world" and scene.get("era") else ""
        lines.append(
            f"날짜: {era}{_value(scene.get('year'), '년')} {_value(scene.get('month'), '월')} "
            f"{_value(scene.get('day'), '일')}"
        )
    elif mode == "elapsed":
        lines.append(f"기준 사건: {_value(scene.get('reference_event'))}")
        lines.append(f"일차: {_value(scene.get('elapsed_day'), '일째')}")
    hour, minute = scene.get("hour"), scene.get("minute")
    if hour is None:
        lines.append("시각: 미지정")
    else:
        lines.append(f"시각: {hour:02d}:{minute:02d}" if minute is not None else f"시각: {hour}시")
    lines.append(f"장소: {_value(scene.get('place'))}")
    lines.append("등장 캐릭터:")
    for state in character_states:
        lines.append(
            f"- {state['name']}: 착장 {_value(state.get('clothing'))} / "
            f"속마음 {_value(state.get('inner_thought'))}"
        )
    return "\n".join(lines)


def build_judge_messages(context: PromptContext, reply_text: str) -> list[dict]:
    user_turn = label_parts(context.current, context.play_name)
    content = "\n\n".join(
        [
            f"[이용자 캐릭터] {context.play_name or '이용자'}",
            "[지금 저장된 장면]\n" + describe_scene(context.scene, context.character_states),
            "[이번 턴 이용자 입력]\n" + user_turn,
            "[이번 턴 AI 응답]\n" + reply_text.strip(),
        ]
    )
    return [{"role": "system", "content": JUDGE_SYSTEM}, {"role": "user", "content": content}]


def get_judge_provider() -> providers.Provider:
    """판단 호출 공급자(판단 전용 대기 시간). 테스트는 이 함수를 바꿔 판단 결과를 정한다."""
    return providers.get_provider(timeout=settings.chat_scene_judge_timeout_seconds)


def judge_turn(
    context: PromptContext, reply_text: str, model: str, provider: str
) -> ScenePatch | None:
    """이번 턴의 장면 변화. 꺼져 있거나 실패·시간 초과·읽기 실패·근거 없음이면 None(장면 그대로).

    어떤 오류도 밖으로 내보내지 않는다. 판단이 잘못돼도 이미 끝난 응답은 정상 저장돼야 한다.
    """
    if not settings.chat_scene_judge_enabled:
        return None
    try:
        request = providers.ProviderRequest(
            model=model,
            provider=provider,
            messages=build_judge_messages(context, reply_text),
            character_names=context.session_character_names,
            last_user_text="",
        )
        deadline = monotonic() + settings.chat_scene_judge_timeout_seconds
        chunks = []
        for chunk in get_judge_provider().stream(request):
            chunks.append(chunk)
            if monotonic() > deadline:
                logger.warning("장면 판단 시간 초과: 장면을 바꾸지 않는다")
                return None
        patch = parse_judgement("".join(chunks), context.session_character_names)
        if patch is None:
            return None
        # 근거 문장은 이번 턴 입력이나 응답에 실제로 있어야 한다(지어낸 근거로 장면을 바꾸지 않게).
        # 근거 위치(이용자/응답)는 판단이 말한 값이 아니라 실제로 찾은 곳으로 정한다.
        if evidence_found(patch.evidence, label_parts(context.current, context.play_name)):
            return replace(patch, evidence_from="user")
        if evidence_found(patch.evidence, reply_text):
            return replace(patch, evidence_from="assistant")
        logger.info("장면 판단 근거가 이번 턴에 없음: 장면을 바꾸지 않는다")
        return None
    except Exception:
        logger.warning("장면 판단 실패: 장면을 바꾸지 않는다", exc_info=True)
        return None
