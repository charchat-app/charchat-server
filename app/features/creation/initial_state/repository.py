"""CREATE-US-04 인트로별 장면·스토리 공통 관계/호감도 SQL."""

SCENE_COLUMNS = (
    "date_mode",
    "era",
    "year_input",
    "month_input",
    "day_input",
    "reference_event",
    "elapsed_day_input",
    "time_format",
    "hour_input",
    "minute_input",
    "meridiem",
    "place",
)


def read_scene_settings(connection, story_id):
    return connection.execute(
        f"""SELECT s.intro_id,{",".join("s." + c for c in SCENE_COLUMNS)}
            FROM intro_scene_settings s JOIN intros i ON i.id=s.intro_id
            WHERE i.story_id=%s""",
        (story_id,),
    ).fetchall()


def read_character_states(connection, story_id):
    return connection.execute(
        """SELECT s.intro_id,s.character_id,s.clothing,s.inner_thought
           FROM intro_character_states s
           JOIN intros i ON i.id=s.intro_id
           JOIN characters c ON c.id=s.character_id
           WHERE i.story_id=%s ORDER BY c.position,c.id""",
        (story_id,),
    ).fetchall()


def write_scene(connection, intro_id, scene):
    connection.execute("DELETE FROM intro_scene_settings WHERE intro_id=%s", (intro_id,))
    connection.execute(
        f"""INSERT INTO intro_scene_settings(intro_id,{",".join(SCENE_COLUMNS)})
            VALUES (%s{",%s" * len(SCENE_COLUMNS)})""",
        (intro_id, *(getattr(scene, c) for c in SCENE_COLUMNS)),
    )
    connection.execute("DELETE FROM intro_character_states WHERE intro_id=%s", (intro_id,))
    for state in scene.characters:
        connection.execute(
            """INSERT INTO intro_character_states(intro_id,character_id,clothing,inner_thought)
               VALUES (%s,%s,%s,%s)""",
            (intro_id, int(state.character_id), state.clothing, state.inner_thought),
        )


def read_relations(connection, story_id):
    return connection.execute(
        """SELECT r.id,r.source_kind,r.source_character_id,r.target_kind,
                  r.target_character_id,r.label
           FROM character_initial_relationships r
           WHERE r.source_character_id IN (SELECT id FROM characters WHERE story_id=%s)
              OR r.target_character_id IN (SELECT id FROM characters WHERE story_id=%s)
           ORDER BY r.id""",
        (story_id, story_id),
    ).fetchall()


def replace_relations(connection, story_id, relations):
    """스토리 공통 관계 전체 교체. 요청 순서대로 다시 넣어 조회 순서를 유지한다."""
    people = "(SELECT id FROM characters WHERE story_id=%s)"
    connection.execute(
        f"""DELETE FROM character_initial_relationships
            WHERE source_character_id IN {people} OR target_character_id IN {people}""",
        (story_id, story_id),
    )
    if not relations:
        return
    # 상한이 캐릭터 50명 기준(최대 2550개)이므로 한 번에 보낸다. 순서는 목록 순서 그대로다.
    with connection.cursor() as cursor:
        cursor.executemany(
            """INSERT INTO character_initial_relationships
               (source_kind,source_character_id,target_kind,target_character_id,label)
               VALUES (%s,%s,%s,%s,%s)""",
            [
                (
                    r.source_kind,
                    r.source_character_id,
                    r.target_kind,
                    r.target_character_id,
                    r.label,
                )
                for r in relations
            ],
        )


def read_affinities(connection, story_id):
    return connection.execute(
        """SELECT a.source_character_id,a.target_kind,a.target_character_id,a.affinity_input
           FROM character_initial_affinities a
           JOIN characters s ON s.id=a.source_character_id
           LEFT JOIN characters t ON t.id=a.target_character_id
           WHERE s.story_id=%s
           ORDER BY s.position,s.id,a.target_kind DESC,t.position,t.id""",
        (story_id,),
    ).fetchall()


def replace_affinities(connection, story_id, affinities):
    """스토리 공통 방향별 호감도 전체 교체. 목록에 없는 조합은 행이 없고 0으로 시작한다."""
    connection.execute(
        """DELETE FROM character_initial_affinities
           WHERE source_character_id IN (SELECT id FROM characters WHERE story_id=%s)""",
        (story_id,),
    )
    if not affinities:
        return
    with connection.cursor() as cursor:
        cursor.executemany(
            """INSERT INTO character_initial_affinities
               (source_character_id,target_kind,target_character_id,affinity_input)
               VALUES (%s,%s,%s,%s)""",
            [
                (a.source_character_id, a.target_kind, a.target_character_id, a.value)
                for a in affinities
            ],
        )
