"""CHAT-US-08 AI 응답 맨 끝 ```info 블록(현재 장면) 떼기·읽기·잇기(KAN-22).

AI는 매 응답 맨 끝에 그 응답이 끝난 시점의 장면(날짜·시간·장소·캐릭터별 착장·속마음)을 info 블록으로
쓴다. 원문은 생성 원문(conversation_generations.text)에 그대로 남고, 말풍선(메시지 부분)에는 info를
뗀 본문만 저장한다. 다음 요청의 [현재 장면]은 세션 시작 값에서 출발해 완료된 응답의 info를 오래된
것부터 차례로 덮어쓴 값이다. 새 표·열은 없다.

앱(스트리밍 중 info 숨김, 상태 카드)도 아래 규칙을 똑같이 쓴다. 번호 규칙이 계약이다.

떼기(split_info)
1. 줄 경계는 str.splitlines 규칙(\\n, \\r\\n, \\r 등)이다. 줄을 비교할 때는 앞뒤 공백을
   JS_TRIM_CHARS(앱 String.prototype.trim과 같은 문자)로 다듬는다.
2. 여는 줄은 다듬은 내용이 정확히 ```info 인 줄이다(소문자, 뒤에 다른 글 없음). 여러 개면 마지막
   여는 줄만 본다.
3. 닫는 줄은 여는 줄 뒤 처음 나오는, 다듬은 내용이 정확히 ``` 인 줄이다.
4. 닫는 줄 뒤에 공백 외 글이 있으면 info가 없다고 본다(원문 전체가 본문).
5. 닫는 줄이 없으면(응답이 블록 중간에 끝남) 여는 줄 다음부터 끝까지가 info다.
6. 본문은 원문에서 여는 줄 시작 앞까지 그대로다. info 텍스트는 여는 줄과 닫는 줄 사이의 줄을
   \\n으로 이은 것이다(빈 블록이면 빈 문자열).
7. 여는 줄이 없으면 info는 없음(None)이고 본문은 원문 그대로다.

읽기(parse_info)
8. info 텍스트를 줄마다 다듬어 읽는다. 키와 값은 반각 콜론(:)으로 나누고, 키·값 앞뒤 공백은
   다듬는다. 값이 비었거나 `-`면 그 항목은 적지 않은 것으로 본다.
9. `날짜:`는 세션의 날짜 방식에 맞는 모양만 읽는다(숫자는 ASCII 0-9).
   - 달력: `2026년 11월 27일`. 연 1~9999, 월 1~12, 일은 그 달의 날 수까지(2월은 28일, 윤년 없음).
   - 세계관: `<연호> 1898년 3월 2일`. 연호는 10자 이내, 연월일은 달력과 같다.
   - 경과일: `<기준 사건> 3일째` 또는 `3일째`. 기준 사건 10자 이내, 일차는 1~999999.
     기준 사건을 쓰지 않으면 기준 사건은 그대로 둔다.
   - 날짜 방식이 없는 세션은 날짜 줄을 읽지 않는다. 날짜 방식은 info로 바뀌지 않는다.
10. `시간:`은 `19:30`(시 0~23, 분 두 자리 00~59) 또는 `19시`(분 모름)만 읽는다.
11. `장소:`는 30자 이내 글이다.
12. 캐릭터 줄은 `[이름] 착장: … | 속마음: …`이다. 이름은 세션 등장 캐릭터 이름과 정확히 같아야 하고,
    모르는 이름(이용자 캐릭터 포함)의 줄은 무시한다. `|`로 나눈 칸마다 `착장:`(300자 이내)·
    `속마음:`(100자 이내)을 읽는다. 값에는 `|`를 쓸 수 없다.
13. 읽을 수 없거나 상한을 넘는 값은 그 항목만 버린다. 같은 항목이 여러 번 나오면 마지막으로 읽은
    값을 쓴다. 키가 아닌 줄은 무시한다. 글자 수는 코드 포인트 기준이다(JS는 [...s].length).

잇기(apply_info)
14. 읽은 항목만 덮어쓰고 나머지는 앞 상태를 그대로 둔다. 시간은 시·분을 함께 바꾼다(`19시`면 분은
    모름). 착장·속마음은 캐릭터별·항목별로 바꾼다.
15. 정상 완료(completed)된 응답의 info만 쓴다. 실패·일부 실패 응답의 info는 쓰지 않는다(서비스).
"""

import re

from app.features.conversation.messaging.parser import JS_TRIM_CHARS

INFO_OPEN = "```info"
INFO_CLOSE = "```"
# 생성 4번(초기 상태)과 같은 달력: 윤년 미지원, 2월 28일 고정.
DAYS_IN_MONTH = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
# 글자 상한. 생성 4번 입력 상한(initial_state LIMITS)과 같다. 넘으면 그 항목은 버린다.
PLACE_MAX_LENGTH = 30
ERA_MAX_LENGTH = 10
REFERENCE_EVENT_MAX_LENGTH = 10
CLOTHING_MAX_LENGTH = 300
INNER_THOUGHT_MAX_LENGTH = 100
YEAR_MAX = 9999
# 런타임 경과일은 생성 상한(9999)을 옮기지 않는다. 숫자로 다룰 수 있는 범위만 둔다.
ELAPSED_DAY_MAX = 999999
DATE_FIELDS = ("era", "year", "month", "day", "reference_event", "elapsed_day")

_KEY_VALUE = re.compile(r"^([^:]+):(.*)$", re.DOTALL)
_YMD = r"([0-9]{1,4})년\s*([0-9]{1,2})월\s*([0-9]{1,2})일"
_CALENDAR = re.compile(rf"^{_YMD}$")
_WORLD = re.compile(rf"^(.+?)\s+{_YMD}$")
_ELAPSED = re.compile(r"^(?:(.+?)\s+)?([0-9]{1,6})일째$")
_CLOCK = re.compile(r"^([0-9]{1,2}):([0-9]{2})$")
_HOUR = re.compile(r"^([0-9]{1,2})시$")
_CHARACTER = re.compile(r"^\[([^\]]+)\](.*)$", re.DOTALL)


def _trim(value: str) -> str:
    return value.strip(JS_TRIM_CHARS)


def split_info(text: str) -> tuple[str, str | None]:
    """응답 원문 → (본문, info 텍스트 또는 None). 규칙 1~7."""
    raw = text.splitlines(keepends=True)  # 본문을 원문 그대로 자르기 위한 줄(줄바꿈 포함)
    lines = text.splitlines()
    opening = None
    for index, line in enumerate(lines):
        if _trim(line) == INFO_OPEN:
            opening = index
    if opening is None:
        return text, None
    closing = next(
        (i for i in range(opening + 1, len(lines)) if _trim(lines[i]) == INFO_CLOSE), None
    )
    if closing is not None and any(_trim(line) for line in lines[closing + 1 :]):
        return text, None
    end = len(lines) if closing is None else closing
    return "".join(raw[:opening]), "\n".join(lines[opening + 1 : end])


def _text(value: str, limit: int) -> str | None:
    value = _trim(value)
    if not value or value == "-" or len(value) > limit:
        return None
    return value


def _ymd(year: str, month: str, day: str) -> tuple[int, int, int] | None:
    y, m, d = int(year), int(month), int(day)
    if not (1 <= y <= YEAR_MAX and 1 <= m <= 12 and 1 <= d <= DAYS_IN_MONTH[m - 1]):
        return None
    return y, m, d


def _date(value: str, date_mode: str | None) -> dict | None:
    date = dict.fromkeys(DATE_FIELDS)
    if date_mode == "calendar" and (match := _CALENDAR.match(value)):
        ymd = _ymd(*match.groups())
    elif date_mode == "world" and (match := _WORLD.match(value)):
        era = _text(match.group(1), ERA_MAX_LENGTH)
        ymd = _ymd(*match.groups()[1:]) if era else None
        date["era"] = era
    elif date_mode == "elapsed" and (match := _ELAPSED.match(value)):
        day = int(match.group(2))
        event = match.group(1)
        if not 1 <= day <= ELAPSED_DAY_MAX:
            return None
        if event is not None:
            event = _text(event, REFERENCE_EVENT_MAX_LENGTH)
            if event is None:
                return None
        return {**date, "mode": date_mode, "reference_event": event, "elapsed_day": day}
    else:
        return None
    if ymd is None:
        return None
    date["year"], date["month"], date["day"] = ymd
    return {**date, "mode": date_mode}


def _time(value: str) -> dict | None:
    if match := _CLOCK.match(value):
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour <= 23 and minute <= 59:
            return {"hour": hour, "minute": minute}
    elif (match := _HOUR.match(value)) and int(match.group(1)) <= 23:
        return {"hour": int(match.group(1)), "minute": None}
    return None


def _character(rest: str, state: dict) -> None:
    for cell in rest.split("|"):
        match = _KEY_VALUE.match(_trim(cell))
        if not match:
            continue
        key, value = _trim(match.group(1)), match.group(2)
        if key == "착장" and (clothing := _text(value, CLOTHING_MAX_LENGTH)):
            state["clothing"] = clothing
        elif key == "속마음" and (thought := _text(value, INNER_THOUGHT_MAX_LENGTH)):
            state["inner_thought"] = thought


def parse_info(info_text: str, date_mode: str | None, character_names: list[str]) -> dict:
    """info 텍스트 → {"date", "time", "place", "characters"}. 규칙 8~13.

    date: {"mode", "era", "year", "month", "day", "reference_event", "elapsed_day"} 또는 None
    time: {"hour", "minute"} 또는 None. place: 글 또는 None.
    characters: 읽은 항목이 하나라도 있는 캐릭터만 세션 캐릭터 순서로
                [{"name", "clothing", "inner_thought"}] (적지 않은 항목은 None).
    """
    parsed: dict = {"date": None, "time": None, "place": None}
    states = {
        name: {"name": name, "clothing": None, "inner_thought": None} for name in character_names
    }
    for raw in info_text.splitlines():
        line = _trim(raw)
        if match := _CHARACTER.match(line):
            state = states.get(_trim(match.group(1)))
            if state is not None:
                _character(match.group(2), state)
            continue
        match = _KEY_VALUE.match(line)
        if not match:
            continue
        key, value = _trim(match.group(1)), _trim(match.group(2))
        if key == "날짜" and (date := _date(value, date_mode)):
            parsed["date"] = date
        elif key == "시간" and (time := _time(value)):
            parsed["time"] = time
        elif key == "장소" and (place := _text(value, PLACE_MAX_LENGTH)):
            parsed["place"] = place
    parsed["characters"] = [
        state
        for name in dict.fromkeys(character_names)
        if (state := states[name])["clothing"] is not None or state["inner_thought"] is not None
    ]
    return parsed


def apply_info(
    scene: dict | None, character_states: list[dict], parsed: dict
) -> tuple[dict, list[dict]]:
    """앞 상태에 읽은 info를 덮어쓴 새 상태(입력은 바꾸지 않는다). 규칙 14."""
    scene = dict(scene or {})
    if parsed["date"]:
        for key in DATE_FIELDS:
            if parsed["date"][key] is not None:
                scene[key] = parsed["date"][key]
    if parsed["time"]:
        scene["hour"] = parsed["time"]["hour"]
        scene["minute"] = parsed["time"]["minute"]
    if parsed["place"]:
        scene["place"] = parsed["place"]
    changed = {row["name"]: row for row in parsed["characters"]}
    states = []
    for state in character_states:
        state = dict(state)
        if (row := changed.get(state["name"])) is not None:
            for key in ("clothing", "inner_thought"):
                if row[key] is not None:
                    state[key] = row[key]
        states.append(state)
    return scene, states


def _date_line(scene: dict) -> str | None:
    mode = scene.get("date_mode")
    ymd = " ".join(
        f"{scene[key]}{unit}"
        for key, unit in (("year", "년"), ("month", "월"), ("day", "일"))
        if scene.get(key) is not None
    )
    if mode == "calendar" and ymd:
        return ymd
    if mode == "world" and (scene.get("era") or ymd):
        return " ".join(x for x in (scene.get("era"), ymd) if x)
    if mode == "elapsed" and scene.get("elapsed_day") is not None:
        # 기준 사건이 비어 있으면 임의 이름('시작')을 붙이지 않는다.
        event = scene.get("reference_event")
        day = scene["elapsed_day"]
        return f"{event} {day}일째" if event else f"{day}일째"
    return None


def format_info_lines(scene: dict | None, character_states: list[dict]) -> list[str]:
    """현재 장면을 info와 같은 줄 형식으로 쓴다. 미지정 줄은 쓰지 않고 캐릭터 줄은 모두 쓴다.

    이 줄을 parse_info로 다시 읽으면 같은 값이 된다(모델이 그대로 옮겨 쓰기 쉽게).
    """
    scene = scene or {}
    lines = []
    if (date := _date_line(scene)) is not None:
        lines.append(f"날짜: {date}")
    if scene.get("hour") is not None:
        # 분이 미지정이면 0분으로 채우지 않는다.
        hour, minute = scene["hour"], scene.get("minute")
        lines.append(f"시간: {hour:02d}:{minute:02d}" if minute is not None else f"시간: {hour}시")
    if scene.get("place"):
        lines.append(f"장소: {scene['place']}")
    for state in character_states:
        clothing = state.get("clothing") or "-"
        thought = state.get("inner_thought") or "-"
        lines.append(f"[{state['name']}] 착장: {clothing} | 속마음: {thought}")
    return lines
