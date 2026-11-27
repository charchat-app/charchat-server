"""CHAT-US-08 AI 응답 맨 끝 ```info 블록(현재 장면) 떼기·읽기·잇기(KAN-22).

AI는 매 응답 맨 끝에 그 응답이 끝난 시점의 장면(날짜·시간·장소·캐릭터별 착장·속마음)을 info 블록으로
쓴다. 원문은 생성 원문(conversation_generations.text)에 그대로 남고, 말풍선(메시지 부분)에는 info를
뗀 본문만 저장한다. 다음 요청의 [현재 장면]은 세션 시작 값에서 출발해 완료된 응답의 info를 오래된
것부터 차례로 덮어쓴 값이다. 새 표·열은 없다.

앱(스트리밍 중 info 숨김, 상태 카드)도 떼기 규칙 1~8을 똑같이 쓴다(서버·앱 공통 계약 A). 읽기 규칙
9~15는 서버만 쓴다(계약 C). 번호 규칙이 계약이다.

떼기(split_info) — 계약 A
1. 줄 나누기: Python `str.splitlines()`와 같은 줄 경계 — `\\r\\n`, `\\n`, `\\r`, `\\v`(U+000B),
   `\\f`(U+000C), U+001C, U+001D, U+001E, U+0085, U+2028, U+2029. (앱의 기존 `splitPythonLines`가
   있으면 그대로 쓴다.)
2. 여는 줄: 앞뒤의 공백(U+0020)·탭(U+0009)만 무시했을 때 정확히 ```info 인 줄.
   대소문자 구분(소문자만). 다른 문자(BOM·NBSP 등)가 붙으면 여는 줄이 아니다.
3. 닫는 줄: 여는 줄 뒤에서 처음 나오는, 앞뒤 공백·탭만 무시했을 때 정확히 ``` 인 줄.
4. 여는 줄이 여러 개면 마지막 여는 줄만 쓴다.
5. 블록 = 마지막 여는 줄부터 그 닫는 줄까지(포함). 닫는 줄이 없으면 여는 줄부터 원문 끝까지.
6. 본문 = 블록 앞의 줄들 + 닫는 줄 뒤의 줄들을 `\\n`으로 이은 것. info 원문 = 두 펜스 사이의 줄들을
   `\\n`으로 이은 것(닫는 줄이 없으면 여는 줄 뒤 전부). 여는 줄이 없으면 본문 = 원문 그대로,
   info = 없음.
7. 스트리밍 중(아직 완료 전 원문)에만 추가: 원문의 마지막 줄(뒤에 줄바꿈이 아직 없는 줄)이 앞쪽
   공백·탭을 뺀 뒤 ```info 의 비어 있지 않은 앞부분(`, ``, ```, ```i, …, ```info)이면 그 줄은
   화면에서 숨긴다(펜스가 반쯤 도착한 것일 수 있다). 완료·실패 확정 원문에는 이 규칙을 쓰지 않는다.
   (앱 전용. 서버는 완료·실패 확정 원문만 다루므로 split_info에 이 규칙이 없다.)
8. 말풍선 나누기(`parse_assistant_text` / `parseAssistantText`)에는 본문만 넣는다. 기존 파서는
   바꾸지 않는다.

읽기(parse_info) — 계약 C, 서버만
9. info 원문을 줄마다 앞뒤 공백(JS_TRIM_CHARS, 앱 String.prototype.trim과 같은 문자)을
   다듬어 읽는다. `키: 값`은 첫 반각 콜론(:)으로 나누고 키·값 앞뒤를 다듬는다.
   키가 아닌 줄은 무시한다.
10. 값이 아닌 것: 빈 값, `<`로 시작하고 `>`로 끝나는 자리 표시 값, `-`, `?`, `미정`, `모름`, `없음`,
    `알 수 없음`, `지정 없음`, `미지정`(앞뒤 공백·마침표 무시). 그 항목은 적지 않은 것으로 보고 직전
    상태를 유지한다. 날짜의 연호·기준 사건이 이런 값이면 그 날짜 줄은 읽지 않는다(직전 날짜 유지).
11. `날짜:`는 세션의 날짜 방식에 맞는 모양만 읽는다. 숫자는 ASCII 0-9, 낱말 사이 공백은 반각
    공백·탭만 인정한다(정규식에 `\\s`를 쓰지 않는다).
    - 달력: `2026년 11월 27일`. 연 1~9999, 월 1~12, 일은 그 달의 날 수까지(2월은 28일, 윤년 없음).
    - 세계관: `<연호> 1898년 3월 2일`. 연호는 10자 이내, 연월일은 달력과 같다.
    - 경과일: `<기준 사건> 3일째` 또는 `3일째`. 기준 사건 10자 이내, 일차는 1~999999.
      기준 사건을 쓰지 않으면 기준 사건은 그대로 둔다.
    - 날짜 방식이 없는 세션은 날짜 줄을 읽지 않는다. 날짜 방식은 info로 바뀌지 않는다.
12. `시간:`은 `19:30`(시 0~23, 분 두 자리 00~59) 또는 `19시`(분 모름)만 읽는다.
13. `장소:`는 30자 이내 글이다.
14. 캐릭터 줄은 `[이름] 착장: … | 속마음: …`이다. `[`로 시작하는 줄에서 `[이름]`의 이름이
    세션 캐릭터 이름 목록과 정확히 일치하는 것을 찾는다(이름에 `]`가 있어도 된다.
    여럿이 맞으면 가장 긴 이름). 맞는 이름이 없는 줄(이용자 캐릭터·모르는 이름)은 무시한다.
    이름 뒤는 `착장: 값`, `착장: 값 | 속마음: 값`, `속마음: 값` 중 하나다. 착장과 속마음은
    처음 나오는 `| 속마음:` 키로 나눈다(값 안의 `|`는 값의 일부다). 키와 콜론 사이,
    `|`와 `속마음` 사이에는 공백·탭만 올 수 있다. 착장은 300자, 속마음은 100자 이내다.
    이 모양이 아니면 그 줄을 무시한다.
15. 읽을 수 없거나 상한을 넘는 값은 그 항목만 버린다. 같은 항목이 여러 번 나오면 마지막으로 읽은
    값을 쓴다. 글자 수는 코드 포인트 기준이다(JS는 [...s].length).

잇기(apply_info)
16. 읽은 항목만 덮어쓰고 나머지는 앞 상태를 그대로 둔다. 시간은 시·분을 함께 바꾼다(`19시`면 분은
    모름). 착장·속마음은 캐릭터별·항목별로 바꾼다.
17. 정상 완료(completed)된 응답의 info만 쓴다. 실패·일부 실패 응답의 info는 쓰지 않는다(서비스).
"""

import re

from app.features.conversation.messaging.parser import JS_TRIM_CHARS

INFO_OPEN = "```info"
INFO_CLOSE = "```"
# 규칙 2·3: 펜스 줄에서 무시하는 문자는 반각 공백과 탭뿐이다(BOM·NBSP 등은 무시하지 않는다).
FENCE_PADDING = " \t"
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
# 규칙 10: 값이 아닌 말(앞뒤 공백·마침표를 뺀 뒤 비교한다).
NOT_A_VALUE = frozenset({"-", "?", "미정", "모름", "없음", "알 수 없음", "지정 없음", "미지정"})

# 규칙 11: 공백은 반각 공백·탭만(Python·JS의 `\s`는 서로 다른 문자를 받는다).
_GAP = "[ \t]"
_KEY_VALUE = re.compile(r"^([^:]+):(.*)$", re.DOTALL)
_YMD = rf"([0-9]{{1,4}})년{_GAP}*([0-9]{{1,2}})월{_GAP}*([0-9]{{1,2}})일"
_CALENDAR = re.compile(rf"^{_YMD}$")
_WORLD = re.compile(rf"^(.+?){_GAP}+{_YMD}$")
_ELAPSED = re.compile(rf"^(?:(.+?){_GAP}+)?([0-9]{{1,6}})일째$")
_CLOCK = re.compile(r"^([0-9]{1,2}):([0-9]{2})$")
_HOUR = re.compile(r"^([0-9]{1,2})시$")
# 규칙 14: 이름 뒤 `착장: 값 | 속마음: 값`. 처음 나오는 `| 속마음:`에서 나눈다(값 안의 `|`는 값).
_CHARACTER_ITEMS = re.compile(
    rf"^(?:착장{_GAP}*:(?P<clothing>.*?))?(?:(?:^|\|){_GAP}*속마음{_GAP}*:(?P<thought>.*))?$",
    re.DOTALL,
)


def _trim(value: str) -> str:
    return value.strip(JS_TRIM_CHARS)


def split_info(text: str) -> tuple[str, str | None]:
    """응답 원문 → (본문, info 원문 또는 None). 규칙 1~6(규칙 7은 앱 스트리밍 전용)."""
    lines = text.splitlines()
    opening = None
    for index, line in enumerate(lines):
        if line.strip(FENCE_PADDING) == INFO_OPEN:
            opening = index
    if opening is None:
        return text, None
    closing = next(
        (
            index
            for index in range(opening + 1, len(lines))
            if lines[index].strip(FENCE_PADDING) == INFO_CLOSE
        ),
        None,
    )
    if closing is None:
        return "\n".join(lines[:opening]), "\n".join(lines[opening + 1 :])
    body = "\n".join(lines[:opening] + lines[closing + 1 :])
    return body, "\n".join(lines[opening + 1 : closing])


def is_value(value: str) -> bool:
    """규칙 10: 적힌 값인가(빈 값·자리 표시·모름 같은 말은 값이 아니다)."""
    core = value.strip(JS_TRIM_CHARS + ".")
    if not core or core in NOT_A_VALUE:
        return False
    return not (core.startswith("<") and core.endswith(">"))


def _text(value: str, limit: int) -> str | None:
    value = _trim(value)
    if not is_value(value) or len(value) > limit:
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


def _character_line(line: str, names: list[str]) -> tuple[str, dict] | None:
    """규칙 14: `[이름] 착장: … | 속마음: …` → (이름, 읽은 항목). 맞는 이름·모양이 아니면 None."""
    name = next((name for name in names if line.startswith(f"[{name}]")), None)
    if name is None:
        return None
    match = _CHARACTER_ITEMS.match(_trim(line[len(name) + 2 :]))
    if not match or (match.group("clothing") is None and match.group("thought") is None):
        return None
    items = {}
    if match.group("clothing") is not None:
        items["clothing"] = _text(match.group("clothing"), CLOTHING_MAX_LENGTH)
    if match.group("thought") is not None:
        items["inner_thought"] = _text(match.group("thought"), INNER_THOUGHT_MAX_LENGTH)
    return name, items


def parse_info(info_text: str, date_mode: str | None, character_names: list[str]) -> dict:
    """info 원문 → {"date", "time", "place", "characters"}. 규칙 9~15.

    date: {"mode", "era", "year", "month", "day", "reference_event", "elapsed_day"} 또는 None
    time: {"hour", "minute"} 또는 None. place: 글 또는 None.
    characters: 읽은 항목이 하나라도 있는 캐릭터만 세션 캐릭터 순서로
                [{"name", "clothing", "inner_thought"}] (적지 않은 항목은 None).
    """
    parsed: dict = {"date": None, "time": None, "place": None}
    states = {
        name: {"name": name, "clothing": None, "inner_thought": None} for name in character_names
    }
    # 이름에 `]`가 있거나 한 이름이 다른 이름의 앞부분이어도 정확히 맞도록 긴 이름부터 본다.
    longest_first = sorted((name for name in states if name), key=len, reverse=True)
    for raw in info_text.splitlines():
        line = _trim(raw)
        if line.startswith("["):
            if (found := _character_line(line, longest_first)) is not None:
                name, items = found
                for key, value in items.items():
                    if value is not None:
                        states[name][key] = value
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
    """앞 상태에 읽은 info를 덮어쓴 새 상태(입력은 바꾸지 않는다). 규칙 16."""
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

    날짜가 완전하면(달력: 연월일, 세계관: 연호와 연월일, 경과일: 일차) 이 줄을 parse_info로 다시
    읽으면 같은 값이 된다(모델이 그대로 옮겨 쓰기 쉽게). 날짜가 일부만 있으면(연만 있는 등)
    날짜 줄은 그대로 보여 주지만 다시 읽히지 않는다(규칙 11). 그때는 앞 날짜가 그대로 유지된다.
    값이 없는 캐릭터 항목은 `-`로 쓴다(값이 아니므로 다시 읽어도 앞 값을 유지한다).
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


def scene_view(scene: dict | None, character_states: list[dict]) -> dict:
    """누적 현재 장면 → parse_info 결과와 같은 모양(API current_scene). 캐릭터는 세션 캐릭터 전원.

    date는 날짜 방식이 있고 날짜 칸이 하나라도 있으면 {"mode", …DATE_FIELDS}, 아니면 None.
    time은 시가 있으면 {"hour", "minute"}, 아니면 None.
    """
    scene = scene or {}
    date = None
    if scene.get("date_mode") and any(scene.get(key) is not None for key in DATE_FIELDS):
        date = {"mode": scene["date_mode"], **{key: scene.get(key) for key in DATE_FIELDS}}
    time = None
    if scene.get("hour") is not None:
        time = {"hour": scene["hour"], "minute": scene.get("minute")}
    return {
        "date": date,
        "time": time,
        "place": scene.get("place") or None,
        "characters": [
            {
                "name": state["name"],
                "clothing": state.get("clothing"),
                "inner_thought": state.get("inner_thought"),
            }
            for state in character_states
        ],
    }
