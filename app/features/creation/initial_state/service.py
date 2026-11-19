"""CREATE-US-04 초기 상태 조회·저장·완성 저장 조건."""

import re

from app.features.creation.common.errors import app_error
from app.features.creation.initial_state import repository as repo
from app.features.creation.initial_state.schemas import ALL_DATE_FIELDS, TIME_FIELDS, IntroScene
from app.features.creation.save_visibility.schemas import FieldIssue


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


def clear_switched_inputs(connection, story_id, intros):
    """CREATE-US-04-AC07: 날짜 방식·시간제를 바꾸면 이전 입력을 넘기지 않는다.

    일반·세계관은 연·월·일 칸을, 12·24시간제는 시·분 칸을 같이 쓴다. 저장된 방식과 요청 방식이
    다른데 이전에 저장한 값과 같은 원문이 그대로 오면 이전 방식의 입력으로 보고 비운다(NULL).
    변환·숨김 보관은 하지 않으며, 새로 입력한 다른 값은 그대로 저장한다.
    완성 저장 검사 전에 호출해 비운 결과로 검사하고 저장한다.
    """
    stored = read_scenes(connection, story_id)
    for intro in intros:
        before = stored.get(intro.id) if intro.id is not None else None
        if before is None:
            continue
        scene = intro.scene
        stale = []
        if before["date_mode"] is not None and scene.date_mode != before["date_mode"]:
            stale += sorted(ALL_DATE_FIELDS)
        if scene.time_format != before["time_format"]:
            stale += TIME_FIELDS
        cleared = {
            name: None
            for name in stale
            if getattr(scene, name) is not None and getattr(scene, name) == before[name]
        }
        if cleared:
            intro.scene = scene.model_copy(update=cleared)


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
    # 자릿수를 상한에 맞춰 제한해 아주 긴 숫자 문자열도 int 변환 없이 거절한다.
    digits = len(str(high))
    return bool(re.fullmatch(rf"[0-9]{{1,{digits}}}", value or "")) and low <= int(value) <= high


def _affinity_ok(value):
    return bool(re.fullmatch(r"-?[0-9]{1,3}", value)) and -100 <= int(value) <= 100


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


TEXT_NAMES = {
    "place": "장소",
    "clothing": "착장",
    "inner_thought": "속마음",
    "era": "연호",
    "reference_event": "기준 사건",
    "label": "관계명",
}


def _too_long(field, value, name):
    if value is None or len(value) <= LIMITS[name]:
        return []
    over = len(value) - LIMITS[name]
    return [
        FieldIssue(
            field=field,
            code="INITIAL_TEXT_TOO_LONG",
            value=str(over),
            message=(
                f"{TEXT_NAMES[name]}은(는) {LIMITS[name]}자 이내로 작성해 주세요. ({over:,}자 초과)"
            ),
        )
    ]


def collect_completed_issues(intros, relations, affinities) -> list[FieldIssue]:
    """공개·비공개 저장 조건. Unicode 코드 포인트 단위, 원문을 바꾸지 않는다.

    날짜·시간은 칸 묶음 단위로 `intros[i].scene.date`·`intros[i].scene.time`에 표시한다.
    """
    issues: list[FieldIssue] = []
    scenes = [intro.scene for intro in intros]
    issues += [
        FieldIssue(
            field=f"intros[{i}].scene.date",
            code="INITIAL_DATE_INVALID",
            message="초기 날짜를 모두 올바르게 채우거나 비워 주세요.",
        )
        for i, scene in enumerate(scenes)
        if not _date_ok(scene)
    ]
    issues += [
        FieldIssue(
            field=f"intros[{i}].scene.time",
            code="INITIAL_TIME_INVALID",
            message="초기 시간을 모두 올바르게 채우거나 비워 주세요.",
        )
        for i, scene in enumerate(scenes)
        if not _time_ok(scene)
    ]
    for i, scene in enumerate(scenes):
        path = f"intros[{i}].scene"
        for name in ("place", "era", "reference_event"):
            issues += _too_long(f"{path}.{name}", getattr(scene, name), name)
        for j, state in enumerate(scene.characters):
            for name in ("clothing", "inner_thought"):
                issues += _too_long(f"{path}.characters[{j}].{name}", getattr(state, name), name)
    for k, relation in enumerate(relations):
        issues += _too_long(f"relations[{k}].label", relation.label, "label")
    issues += [
        FieldIssue(
            field=f"affinities[{k}].value",
            code="AFFINITY_INVALID",
            value=affinity.value,
            message="초기 호감도는 -100~100 사이 정수로 입력해 주세요.",
        )
        for k, affinity in enumerate(affinities)
        if not _affinity_ok(affinity.value)
    ]
    return issues
