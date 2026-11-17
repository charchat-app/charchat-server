"""CREATE-US-03 인트로 ID를 유지하는 변경·삭제·정렬 SQL."""


def read_intros(connection, story_id):
    return connection.execute(
        "SELECT id,title,position FROM intros WHERE story_id=%s ORDER BY position,id",
        (story_id,),
    ).fetchall()


def read_messages(connection, story_id):
    return connection.execute(
        """SELECT m.id,m.intro_id,m.speaker_kind,m.character_id,m.body,m.position
           FROM intro_messages m JOIN intros i ON i.id=m.intro_id
           WHERE i.story_id=%s ORDER BY i.position,m.position,m.id""",
        (story_id,),
    ).fetchall()


def write_collection(connection, story_id, intros, old_intro_ids, old_message_ids):
    keep_intros = [int(i.id) for i in intros if i.id is not None]
    keep_messages = [int(m.id) for i in intros for m in i.messages if m.id is not None]
    removed_messages = sorted(old_message_ids - set(keep_messages))
    if removed_messages:
        connection.execute("DELETE FROM intro_messages WHERE id=ANY(%s)", (removed_messages,))
    removed_intros = sorted(old_intro_ids - set(keep_intros))
    if removed_intros:
        # 4번 장면·캐릭터 상태는 FK ON DELETE CASCADE로 인트로와 함께 제거된다.
        connection.execute(
            "DELETE FROM intros WHERE story_id=%s AND id=ANY(%s)", (story_id, removed_intros)
        )
    result = []
    for position, intro in enumerate(intros):
        intro_id = int(intro.id) if intro.id is not None else None
        if intro_id is None:
            intro_id = connection.execute(
                "INSERT INTO intros(story_id,title,position) VALUES (%s,%s,%s) RETURNING id",
                (story_id, intro.title, position),
            ).fetchone()["id"]
        else:
            connection.execute(
                "UPDATE intros SET title=%s,position=%s WHERE id=%s AND story_id=%s",
                (intro.title, position, intro_id, story_id),
            )
        messages = []
        for order, message in enumerate(intro.messages):
            character_id = int(message.character_id) if message.character_id is not None else None
            values = (message.speaker_kind, character_id, message.body, order)
            if message.id is None:
                message_id = connection.execute(
                    """INSERT INTO intro_messages
                       (intro_id,speaker_kind,character_id,body,position)
                       VALUES (%s,%s,%s,%s,%s) RETURNING id""",
                    (intro_id, *values),
                ).fetchone()["id"]
            else:
                message_id = int(message.id)
                connection.execute(
                    """UPDATE intro_messages
                       SET speaker_kind=%s,character_id=%s,body=%s,position=%s
                       WHERE id=%s AND intro_id=%s""",
                    (*values, message_id, intro_id),
                )
            messages.append({**message.model_dump(), "id": str(message_id)})
        result.append(
            {
                "id": str(intro_id),
                "client_key": intro.client_key,
                "title": intro.title,
                "messages": messages,
            }
        )
    return result
