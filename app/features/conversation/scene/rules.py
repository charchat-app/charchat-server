"""CHAT-US-08 장면 변화 판단 결과를 세션 현재 장면에 적용하는 순수 규칙.

판단(무엇이 바뀌었나)은 AI가 하고, 이 모듈은 정해진 항목만 정해진 규칙으로 바꾼다.

규칙(KAN-40 설계 초안 §5)
- 바뀐 것만 보낸다. null·빠진 항목은 그대로 둔다(근거 없이는 바꾸지 않는다).
- 장소·시각·캐릭터별 착장·속마음은 보낸 값으로 바꾼다. 미지정이던 값도 분명히 정해지면 채운다.
- 근거 문장이 없으면 바꾸지 않는다.
  근거는 이번 턴 입력이나 응답에 실제로 있어야 한다(judge에서 확인).
- 날짜(달력·세계관): 날짜를 직접 보내면 그 날짜, 지난 날 수만 보내면 저장된 날짜에서 센다.
  시각이 거꾸로 가면(23시 → 1시) 하루가 지난 것으로 본다.
  연도 없이 더 이른 날짜로 가면 다음 해다.
  달력은 생성 4번과 같다(2월은 28일, 윤년 없음). 저장된 월·일이 없으면 날짜를 만들어 내지 않는다.
- 경과일: 지난 날 수만큼 일차를 늘린다. 새 기준 사건이 시작되면 그 사건으로 바꾸고 1일째부터 센다.
- 날짜 방식(달력·세계관·경과일) 자체는 바꾸지 않는다. 방식이 없는 세션은 날짜를 다루지 않는다.
- 한 번 정해진 값을 미지정으로 되돌리지 않는다. 예외: 시각이 바뀌었는데 분을 모르면 분은 비운다.
- 글자 상한은 생성 4번과 같다(장소 30, 기준 사건 10, 착장 300, 속마음 100). 연도는 9999까지.
"""

import json
import re
from dataclasses import dataclass, field

# 생성 4번(initial_state)과 같은 달력: 윤년 미지원, 2월 28일 고정.
DAYS_IN_MONTH = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
# 판단 결과 글자 상한. 생성 4번 입력 상한(initial_state LIMITS)과 같다. 넘으면 그 항목은 버린다.
PLACE_MAX_LENGTH = 30
REFERENCE_EVENT_MAX_LENGTH = 10
CLOTHING_MAX_LENGTH = 300
INNER_THOUGHT_MAX_LENGTH = 100
EVIDENCE_MAX_LENGTH = 500
YEAR_MAX = 9999
DAYS_PASSED_MAX = 3650
SCENE_FIELDS = (
    "date_mode",
    "era",
    "year",
    "month",
    "day",
    "reference_event",
    "elapsed_day",
    "hour",
    "minute",
    "place",
)


@dataclass(frozen=True)
class CharacterPatch:
    name: str
    clothing: str | None = None
    inner_thought: str | None = None


@dataclass(frozen=True)
class ScenePatch:
    place: str | None = None
    year: int | None = None
    month: int | None = None
    day: int | None = None
    days_passed: int | None = None
    hour: int | None = None
    minute: int | None = None
    new_reference_event: str | None = None
    characters: tuple[CharacterPatch, ...] = field(default_factory=tuple)
    evidence: str = ""
    evidence_from: str = "assistant"  # user | assistant


def _text(value, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or "\x00" in value or len(value) > limit:
        return None
    return value


def _int(value, low: int, high: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if low <= value <= high else None


def _extract_json(text: str):
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        text = text[start : end + 1]
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def parse_judgement(text: str, character_names: list[str]) -> ScenePatch | None:
    """판단 원문(JSON)을 검증한 패치로 바꾼다. 바뀐 것이 없거나 읽을 수 없으면 None."""
    data = _extract_json(text or "")
    if data is None or data.get("changed") is not True:
        return None
    raw_date = data.get("date")
    date = raw_date if isinstance(raw_date, dict) else {}
    month = _int(date.get("month"), 1, 12)
    day = _int(date.get("day"), 1, DAYS_IN_MONTH[month - 1]) if month else None
    year = _int(date.get("year"), 1, YEAR_MAX) if month and day else None
    if date.get("year") is not None and year is None:
        # 연도를 보냈는데 쓸 수 없으면 그 날짜 전체를 버린다.
        month = day = None
    raw_time = data.get("time")
    time = raw_time if isinstance(raw_time, dict) else {}
    hour = _int(time.get("hour"), 0, 23)
    minute = _int(time.get("minute"), 0, 59) if hour is not None else None
    names = set(character_names)
    characters = []
    seen = set()
    raw_characters = data.get("characters")
    for row in raw_characters if isinstance(raw_characters, list) else []:
        name = row.get("name") if isinstance(row, dict) else None
        if not isinstance(name, str) or name not in names or name in seen:
            continue
        patch = CharacterPatch(
            name=name,
            clothing=_text(row.get("clothing"), CLOTHING_MAX_LENGTH),
            inner_thought=_text(row.get("inner_thought"), INNER_THOUGHT_MAX_LENGTH),
        )
        if patch.clothing is not None or patch.inner_thought is not None:
            seen.add(row["name"])
            characters.append(patch)
    evidence = _text(data.get("evidence"), EVIDENCE_MAX_LENGTH)
    if evidence is None:
        # 근거 없이는 바꾸지 않는다.
        return None
    patch = ScenePatch(
        place=_text(data.get("place"), PLACE_MAX_LENGTH),
        year=year,
        month=month if day else None,
        day=day,
        days_passed=_int(data.get("days_passed"), 1, DAYS_PASSED_MAX),
        hour=hour,
        minute=minute,
        new_reference_event=_text(data.get("new_reference_event"), REFERENCE_EVENT_MAX_LENGTH),
        characters=tuple(characters),
        evidence=evidence,
        evidence_from="user" if data.get("evidence_from") == "user" else "assistant",
    )
    empty = ScenePatch(evidence=patch.evidence, evidence_from=patch.evidence_from)
    return None if patch == empty else patch


_NOISE = re.compile(r"[\s*\"'“”‘’.,!?…·~\-]+")


def evidence_found(evidence: str, *sources: str) -> bool:
    """근거 문장이 이번 턴 입력이나 응답에 실제로 있는지(공백·기호를 빼고 비교)."""
    needle = _NOISE.sub("", evidence)
    return bool(needle) and any(needle in _NOISE.sub("", source or "") for source in sources)


def advance_date(year: int | None, month: int, day: int, days: int):
    """생성 4번 달력(윤년 없음)으로 days일 뒤 날짜. 연도를 모르면 연도는 그대로 None이다."""
    for _ in range(days):
        day += 1
        if day > DAYS_IN_MONTH[month - 1]:
            day = 1
            month += 1
            if month > 12:
                month = 1
                if year is not None:
                    year += 1
    return year, month, day


def apply_patch(scene: dict | None, characters: list[dict], patch: ScenePatch):
    """새 장면과 바뀐 캐릭터 상태를 돌려준다. 입력은 바꾸지 않는다.

    characters: [{"id", "name", "clothing", "inner_thought"}]
    반환: (new_scene, changed_characters[{"id","name","clothing","inner_thought"}])
    """
    new = {key: (scene or {}).get(key) for key in SCENE_FIELDS}
    if patch.place is not None:
        new["place"] = patch.place
    days = patch.days_passed
    explicit_date = patch.month is not None and patch.day is not None
    if (
        not days
        and not explicit_date
        and patch.new_reference_event is None
        and patch.hour is not None
        and new["hour"] is not None
        and patch.hour < new["hour"]
    ):
        # 시각이 거꾸로 가면(23시 → 1시) 자정을 넘긴 것으로 보고 하루를 더한다.
        days = 1
    mode = new["date_mode"]
    if mode in ("calendar", "world"):
        if explicit_date:
            year = patch.year
            if (
                year is None
                and new["year"] is not None
                and new["month"] is not None
                and new["day"] is not None
                and (patch.month, patch.day) < (new["month"], new["day"])
            ):
                # 연도 없이 더 이른 달·일로 건너뛰면 다음 해다(12월 31일 → 1월 1일).
                year = new["year"] + 1
            if year is None or year <= YEAR_MAX:
                new["month"], new["day"] = patch.month, patch.day
                if year is not None:
                    new["year"] = year
        elif days and new["month"] is not None and new["day"] is not None:
            year, month, day = advance_date(new["year"], new["month"], new["day"], days)
            if year is None or year <= YEAR_MAX:
                new["year"], new["month"], new["day"] = year, month, day
    elif mode == "elapsed":
        if patch.new_reference_event is not None:
            new["reference_event"] = patch.new_reference_event
            new["elapsed_day"] = 1
        elif days and new["elapsed_day"] is not None:
            new["elapsed_day"] += days
    if patch.hour is not None:
        # 같은 시각이면 저장된 분을 지운다고 보지 않는다. 다른 시각이면 분은 판단 결과를 따른다.
        if patch.minute is not None or patch.hour != new["hour"]:
            new["minute"] = patch.minute
        new["hour"] = patch.hour
    changed_characters = []
    by_name = {}
    for row in characters:
        by_name.setdefault(row["name"], row)
    for change in patch.characters:
        row = by_name.get(change.name)
        if row is None:
            continue
        updated = dict(row)
        if change.clothing is not None:
            updated["clothing"] = change.clothing
        if change.inner_thought is not None:
            updated["inner_thought"] = change.inner_thought
        if updated != row:
            changed_characters.append(updated)
    return new, changed_characters
