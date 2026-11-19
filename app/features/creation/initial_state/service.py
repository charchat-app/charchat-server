"""CREATE-US-04 초기 상태 조회·저장·완성 저장 조건."""

import re

from app.features.creation.common.errors import app_error
from app.features.creation.initial_state import repository as repo
from app.features.creation.initial_state.schemas import IntroScene


def empty_scene():
    """행이 없는 인트로의 장면: 날짜·시간·장소·캐릭터 상태 모두 미지정."""
    return IntroScene().model_dump()


LIMITS = {
    "place": 30,
    "clothing": 300,
    "inner_thought": 100,
    "era": 10,
    "reference_event": 10,
    "label": 10,
}
DAYS_IN_MONTH = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]  # 윤년 미지원, 2월 28일 고정


def read_scenes(connection, story_id):
    """intro_id → 장면 dict. 행이 없으면 미지정 장면이다."""
    scenes = {
        row["intro_id"]: {k: v for k, v in row.items() if k != "intro_id"} | {"characters": []}
        for row in repo.read_scene_settings(connection, story_id)
    }
    for row in repo.read_character_states(connection, story_id):
        scenes.setdefault(row["intro_id"], empty_scene())["characters"].append(
            {
                "character_id": row["character_id"],
                "clothing": row["clothing"],
                "inner_thought": row["inner_thought"],
            }
        )
    return scenes


def check_scene_people(intros, character_ids):
    for intro in intros:
        if any(state.character_id not in character_ids for state in intro.scene.characters):
            raise app_error(
                422, "INVALID_SCENE_CHARACTER", "같은 스토리의 캐릭터 상태만 저장할 수 있습니다."
            )


def write_scene(connection, intro_id, scene):
    repo.write_scene(connection, intro_id, scene)
    return scene.model_dump()


def read_relations(connection, story_id):
    return [
        {
            "client_key": f"relation-{row['id']}",
            "source_kind": row["source_kind"],
            "source_character_id": row["source_character_id"],
            "target_kind": row["target_kind"],
            "target_character_id": row["target_character_id"],
            "label": row["label"],
        }
        for row in repo.read_relations(connection, story_id)
    ]


def read_affinities(connection, story_id):
    return [
        {
            "source_character_id": row["source_character_id"],
            "target_kind": row["target_kind"],
            "target_character_id": row["target_character_id"],
            # 열 기본값이 '0'이고, NULL은 지운 값("")으로 되돌려 원문을 보존한다.
            "value": row["affinity_input"] if row["affinity_input"] is not None else "",
        }
        for row in repo.read_affinities(connection, story_id)
    ]


def write_affinities(connection, story_id, affinities, character_ids):
    for affinity in affinities:
        for character in (affinity.source_character_id, affinity.target_character_id):
            if character is not None and character not in character_ids:
                raise app_error(
                    422,
                    "INVALID_AFFINITY_PERSON",
                    "같은 스토리의 인물 사이 호감도만 저장할 수 있습니다.",
                )
    repo.replace_affinities(connection, story_id, affinities)
    return [affinity.model_dump() for affinity in affinities]


def write_relations(connection, story_id, relations, character_ids):
    for relation in relations:
        for character in (relation.source_character_id, relation.target_character_id):
            if character is not None and character not in character_ids:
                raise app_error(
                    422,
                    "INVALID_RELATION_PERSON",
                    "같은 스토리의 인물만 관계로 연결할 수 있습니다.",
                )
    repo.replace_relations(connection, story_id, relations)
    return [relation.model_dump() for relation in relations]


def _integer_in(value, low, high):
    return bool(re.fullmatch(r"[0-9]+", value or "")) and low <= int(value) <= high


def _date_ok(scene):
    values = [getattr(scene, f) for f in ("era", "year_input", "month_input", "day_input")]
    values += [scene.reference_event, scene.elapsed_day_input]
    if scene.date_mode is None or all(v is None for v in values):
        return True  # 날짜 전체 미지정 허용
    if scene.date_mode == "elapsed":
        return bool((scene.reference_event or "").strip()) and _integer_in(
            scene.elapsed_day_input, 1, 9999
        )
    if scene.date_mode == "world" and not (scene.era or "").strip():
        return False
    if not _integer_in(scene.year_input, 1, 9999) or not _integer_in(scene.month_input, 1, 12):
        return False
    return _integer_in(scene.day_input, 1, DAYS_IN_MONTH[int(scene.month_input) - 1])


def _time_ok(scene):
    if scene.hour_input is None and scene.minute_input is None and scene.meridiem is None:
        return True  # 시간 전체 미지정 허용
    low, high = (0, 23) if scene.time_format == "24h" else (1, 12)
    if scene.time_format == "12h" and scene.meridiem is None:
        return False
    return _integer_in(scene.hour_input, low, high) and _integer_in(scene.minute_input, 0, 59)


def _over(value, name):
    return value is not None and len(value) > LIMITS[name]


def validate_completed(intros, relations, affinities):
    """공개·비공개 저장 조건. Unicode 코드 포인트 단위, 원문을 바꾸지 않는다."""
    scenes = [intro.scene for intro in intros]
    if not all(_date_ok(scene) for scene in scenes):
        raise app_error(
            422, "INITIAL_DATE_INVALID", "초기 날짜를 모두 올바르게 채우거나 비워 주세요."
        )
    if not all(_time_ok(scene) for scene in scenes):
        raise app_error(
            422, "INITIAL_TIME_INVALID", "초기 시간을 모두 올바르게 채우거나 비워 주세요."
        )
    if (
        any(
            _over(s.place, "place")
            or _over(s.era, "era")
            or _over(s.reference_event, "reference_event")
            for s in scenes
        )
        or any(
            _over(c.clothing, "clothing") or _over(c.inner_thought, "inner_thought")
            for s in scenes
            for c in s.characters
        )
        or any(_over(r.label, "label") for r in relations)
    ):
        raise app_error(
            422,
            "INITIAL_TEXT_TOO_LONG",
            "장소·착장·속마음·관계명·연호·기준 사건의 글자 수 한도를 확인해 주세요.",
        )
    if any(
        not (re.fullmatch(r"-?[0-9]+", a.value) and -100 <= int(a.value) <= 100) for a in affinities
    ):
        raise app_error(
            422, "AFFINITY_INVALID", "초기 호감도는 -100~100 사이 정수로 입력해 주세요."
        )
