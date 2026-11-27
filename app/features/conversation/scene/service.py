"""CHAT-US-08 장면 유지·변화(KAN-22). 설계: KAN-40 초안(나래님 확인 전 추천안으로 구현).

- 정상 완료된 턴에서만 장면을 바꾼다. 응답 저장과 같은 트랜잭션에서 적용해, 응답이 저장되지 않으면
  장면도 바뀌지 않는다(실패·일부 실패·저장 실패 턴은 무변경).
- 바뀐 값마다 변경 기록(전·후 상태, 근거 메시지·문장)을 남긴다.
  재생성 후보 전환(10번)·구간 삭제(12번)·응답 수정(11번)에서 '그때의 장면'으로 돌아갈 때 쓴다.
"""

import logging

from psycopg import Connection

from app.features.conversation.scene import repository
from app.features.conversation.scene.rules import SCENE_FIELDS, ScenePatch, apply_patch

logger = logging.getLogger(__name__)


def _state(scene: dict, characters: list[dict]) -> dict:
    return {
        "scene": {key: scene.get(key) for key in SCENE_FIELDS},
        "characters": [
            {
                "id": row["id"],
                "name": row["name"],
                "clothing": row["clothing"],
                "inner_thought": row["inner_thought"],
            }
            for row in characters
        ],
    }


def apply_turn_change(
    conn: Connection,
    *,
    session_id: int,
    generation_id: int,
    user_message_id: int,
    assistant_message_id: int,
    patch: ScenePatch | None,
) -> int | None:
    """정상 완료 저장 트랜잭션 안에서 장면 변화를 적용한다. 바뀐 것이 없으면 None.

    적용 중 오류가 나도 응답 저장은 지킨다(세이브포인트로 장면 변경만 되돌린다).
    """
    if patch is None:
        return None
    try:
        with conn.transaction():
            row = repository.get_scene_for_update(conn, session_id)
            if row is None:
                return None
            scene = dict(row)
            characters = [
                dict(r) for r in repository.list_character_states_for_update(conn, session_id)
            ]
            new_scene, changed = apply_patch(scene, characters, patch)
            if new_scene == {key: scene.get(key) for key in SCENE_FIELDS} and not changed:
                return None
            if new_scene != {key: scene.get(key) for key in SCENE_FIELDS}:
                repository.update_scene(conn, session_id, new_scene)
            for state in changed:
                repository.update_character_state(conn, session_id, state)
            by_id = {row["id"]: row for row in changed}
            after_characters = [by_id.get(row["id"], row) for row in characters]
            return repository.insert_change(
                conn,
                session_id=session_id,
                generation_id=generation_id,
                assistant_message_id=assistant_message_id,
                evidence_message_id=(
                    user_message_id if patch.evidence_from == "user" else assistant_message_id
                ),
                evidence_from=patch.evidence_from,
                evidence=patch.evidence,
                before=_state(scene, characters),
                after=_state(new_scene, after_characters),
            )
    except Exception:
        logger.exception("장면 변경 적용 실패: generation=%s", generation_id)
        return None
