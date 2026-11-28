"""CHAT-US-02 테스트 도우미(요청 보내기·가짜 공급자)."""

STORIES = "/api/v1/conversation/stories"
SESSIONS = "/api/v1/conversation/sessions"
GENERATIONS = "/api/v1/conversation/generations"
AUTHOR = {"X-User-Id": "1"}
PLAYER = {"X-User-Id": "2"}


class RecordingProvider:
    """모델 요청 메시지를 기록하고 정해진 응답 조각을 보낸다(외부 호출 없음)."""

    def __init__(self, chunks=None, error=None, error_after=None):
        self.requests = []
        self.chunks = chunks
        self.error = error
        self.error_after = error_after

    def stream(self, request):
        self.requests.append(request)
        if self.error is not None and self.error_after is None:
            raise self.error
        chunks = self.chunks
        if chunks is None:
            name = request.character_names[0]
            chunks = [f"@{name}: 응답 ", f"{len(self.requests)}번\n@나레이터: 끝"]
        for index, chunk in enumerate(chunks):
            if self.error is not None and index == self.error_after:
                raise self.error
            yield chunk


def start_session(client, story, intro, headers=PLAYER) -> int:
    response = client.post(
        f"{STORIES}/{story}/sessions",
        headers=headers,
        json={"intro_id": intro, "profile_kind": "preset", "profile_id": 1, "play_name": ""},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def session_characters(client, session_id, headers=PLAYER) -> dict[str, int]:
    detail = client.get(f"{SESSIONS}/{session_id}", headers=headers).json()
    return {row["name"]: row["id"] for row in detail["characters"]}


def send(client, session_id, parts, request_id="req-00000001", headers=PLAYER):
    return client.post(
        f"{SESSIONS}/{session_id}/messages",
        headers=headers,
        json={"request_id": request_id, "parts": parts},
    )


def user_part(body):
    return {"speaker_kind": "user", "character_id": None, "body": body}


def info_reply(body: str, info: str | None) -> list[str]:
    """CHAT-US-08 응답 조각. info가 있으면 본문 뒤 맨 끝에 ```info 블록을 붙인다."""
    text = body if info is None else f"{body}\n\n```info\n{info}\n```"
    return [text[:10], text[10:]]


def scene_block(request) -> str:
    """CHAT-US-08: 모델 요청 마지막 user 메시지의 [현재 장면] 블록."""
    content = request.messages[-1]["content"]
    return "[현재 장면]\n" + content.split("[현재 장면]\n")[-1].split("\n\n")[0]
