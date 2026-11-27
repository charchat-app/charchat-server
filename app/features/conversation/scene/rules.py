"""CHAT-US-08 장면 변화 판단 결과를 세션 현재 장면에 적용하는 순수 규칙.

판단(무엇이 바뀌었나)은 AI가 하고, 이 모듈은 정해진 항목만 정해진 규칙으로 바꾼다.

규칙(KAN-40 설계 초안 §5)
- 바뀐 것만 보낸다. null·빠진 항목은 그대로 둔다(근거 없이는 바꾸지 않는다).
- 장소·시각·캐릭터별 착장·속마음은 보낸 값으로 바꾼다. 미지정이던 값도 분명히 정해지면 채운다.
- 날짜(달력·세계관): 날짜를 직접 보내면 그 날짜, 지난 날 수만 보내면 저장된 날짜에서 센다.
  달력은 생성 4번과 같다(2월은 28일, 윤년 없음). 저장된 월·일이 없으면 날짜를 만들어 내지 않는다.
- 경과일: 지난 날 수만큼 일차를 늘린다. 새 기준 사건이 시작되면 그 사건으로 바꾸고 1일째부터 센다.
- 날짜 방식(달력·세계관·경과일) 자체는 바꾸지 않는다. 방식이 없는 세션은 날짜를 다루지 않는다.
- 한 번 정해진 값을 미지정으로 되돌리지 않는다.
"""

import json
import re
from dataclasses import dataclass, field

# 생성 4번(initial_state)과 같은 달력: 윤년 미지원, 2월 28일 고정.
DAYS_IN_MONTH = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
# 판단 결과로 받는 글자의 상한(임시값). 넘으면 그 항목은 버린다.
TEXT_MAX_LENGTH = 200
STATE_TEXT_MAX_LENGTH = 500
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
    year = _int(date.get("year"), 1, 99999) if month and day else None
    raw_time = data.get("time")
    time = raw_time if isinstance(raw_time, dict) else {}
    hour = _int(time.get("hour"), 0, 23)
    minute = _int(time.get("minute"), 0, 59) if hour is not None else None
    names = set(character_names)
    characters = []
    seen = set()
    raw_characters = data.get("characters")
    for row in raw_characters if isinstance(raw_characters, list) else []:
        if not isinstance(row, dict) or row.get("name") not in names or row["name"] in seen:
            continue
        patch = CharacterPatch(
            name=row["name"],
            clothing=_text(row.get("clothing"), STATE_TEXT_MAX_LENGTH),
            inner_thought=_text(row.get("inner_thought"), STATE_TEXT_MAX_LENGTH),
        )
        if patch.clothing is not None or patch.inner_thought is not None:
            seen.add(row["name"])
            characters.append(patch)
    patch = ScenePatch(
        place=_text(data.get("place"), TEXT_MAX_LENGTH),
        year=year,
        month=month if day else None,
        day=day,
        days_passed=_int(data.get("days_passed"), 1, DAYS_PASSED_MAX),
        hour=hour,
        minute=minute,
        new_reference_event=_text(data.get("new_reference_event"), TEXT_MAX_LENGTH),
        characters=tuple(characters),
        evidence=_text(data.get("evidence"), STATE_TEXT_MAX_LENGTH) or "",
        evidence_from="user" if data.get("evidence_from") == "user" else "assistant",
    )
    empty = ScenePatch(evidence=patch.evidence, evidence_from=patch.evidence_from)
    return None if patch == empty else patch


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
    mode = new["date_mode"]
    if mode in ("calendar", "world"):
        if patch.month is not None and patch.day is not None:
            new["month"], new["day"] = patch.month, patch.day
            if patch.year is not None:
                new["year"] = patch.year
        elif patch.days_passed and new["month"] is not None and new["day"] is not None:
            new["year"], new["month"], new["day"] = advance_date(
                new["year"], new["month"], new["day"], patch.days_passed
            )
    elif mode == "elapsed":
        if patch.new_reference_event is not None:
            new["reference_event"] = patch.new_reference_event
            new["elapsed_day"] = 1
        elif patch.days_passed and new["elapsed_day"] is not None:
            new["elapsed_day"] += patch.days_passed
    if patch.hour is not None:
        new["hour"], new["minute"] = patch.hour, patch.minute
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
