"""CREATE-US-02 퓨샷 ID를 유지하는 변경·삭제·정렬 SQL."""


def read_characters(connection, story_id):
    return connection.execute(
        "SELECT id,name,position FROM characters WHERE story_id=%s ORDER BY position,id",
        (story_id,),
    ).fetchall()


def read_groups(connection, story_id):
    return connection.execute(
        "SELECT id,position FROM fewshots WHERE story_id=%s ORDER BY position,id",
        (story_id,),
    ).fetchall()


def read_messages(connection, story_id):
    return connection.execute(
        """SELECT m.id,m.fewshot_id,m.speaker_kind,m.character_id,m.body,m.position
           FROM fewshot_messages m JOIN fewshots f ON f.id=m.fewshot_id
           WHERE f.story_id=%s ORDER BY f.position,m.position,m.id""",
        (story_id,),
    ).fetchall()


def write_collection(connection, story_id, groups, old_group_ids, old_message_ids):
    keep_groups = [int(g.id) for g in groups if g.id is not None]
    keep_messages = [int(m.id) for g in groups for m in g.messages if m.id is not None]
    removed_messages = sorted(old_message_ids - set(keep_messages))
    if removed_messages:
        connection.execute("DELETE FROM fewshot_messages WHERE id=ANY(%s)", (removed_messages,))
    removed_groups = sorted(old_group_ids - set(keep_groups))
    if removed_groups:
        connection.execute(
            "DELETE FROM fewshots WHERE story_id=%s AND id=ANY(%s)", (story_id, removed_groups)
        )
    result = []
    for position, group in enumerate(groups):
        group_id = int(group.id) if group.id is not None else None
        if group_id is None:
            group_id = connection.execute(
                "INSERT INTO fewshots(story_id,position) VALUES (%s,%s) RETURNING id",
                (story_id, position),
            ).fetchone()["id"]
        else:
            connection.execute(
                "UPDATE fewshots SET position=%s WHERE id=%s AND story_id=%s",
                (position, group_id, story_id),
            )
        messages = []
        for order, message in enumerate(group.messages):
            character_id = int(message.character_id) if message.character_id is not None else None
            values = (message.speaker_kind, character_id, message.body, order)
            if message.id is None:
                message_id = connection.execute(
                    """INSERT INTO fewshot_messages
                       (fewshot_id,speaker_kind,character_id,body,position)
                       VALUES (%s,%s,%s,%s,%s) RETURNING id""",
                    (group_id, *values),
                ).fetchone()["id"]
            else:
                message_id = int(message.id)
                connection.execute(
                    """UPDATE fewshot_messages
                       SET speaker_kind=%s,character_id=%s,body=%s,position=%s
                       WHERE id=%s AND fewshot_id=%s""",
                    (*values, message_id, group_id),
                )
            messages.append({**message.model_dump(), "id": str(message_id)})
        result.append({"id": str(group_id), "client_key": group.client_key, "messages": messages})
    return result
