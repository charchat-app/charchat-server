"""CHAT-US-02 AI 응답 원문을 발화자별 부분으로 나눈다(앱 parseAssistantText와 같은 규칙).

규칙
- 줄 맨 앞의 `@이름:` 태그가 새 부분을 시작한다.
  이름은 세션 등장 캐릭터 이름 또는 `나레이터`만 인정한다.
- 첫 태그 앞의 글과 알 수 없는 이름(유저 플레이 이름 포함)의 태그 줄은 나레이터 부분으로 둔다.
  알 수 없는 태그 줄은 원문 그대로(태그 포함) 새 나레이터 부분이 된다. 내용은 버리지 않는다.
- 태그가 아닌 줄(`@나`처럼 콜론이 없는 줄 포함)은 앞 부분 본문의 다음 줄로 이어 붙인다.
- 같은 발화자가 연속해도 태그마다 별도 부분이다. 태그 뒤 공백 한 칸과 본문 앞뒤 빈 줄·공백은 뺀다.
- 공백만 남은 부분은 버린다. 본문 안의 `*서술*`, `**강조**` 기호는 그대로 둔다.
"""

import re
from dataclasses import dataclass

NARRATOR_TAG = "나레이터"
_TAG = re.compile(r"^@([^:\n]+?):[ \t]?(.*)$")


@dataclass(frozen=True)
class ParsedPart:
    speaker_kind: str  # "character" | "narrator"
    character_name: str | None
    body: str


def parse_assistant_text(text: str, character_names: list[str]) -> list[ParsedPart]:
    names = set(character_names)
    blocks: list[list] = []  # [kind, name, lines]
    # \r\n·\r 등 모든 줄 경계를 같은 줄바꿈으로 본다(앱 splitPythonLines와 같은 규칙).
    for line in text.splitlines():
        match = _TAG.match(line)
        if match and (match.group(1) in names or match.group(1) == NARRATOR_TAG):
            name = match.group(1)
            if name == NARRATOR_TAG:
                blocks.append(["narrator", None, [match.group(2)]])
            else:
                blocks.append(["character", name, [match.group(2)]])
        elif match or not blocks:
            # 알 수 없는 태그(유저 이름 포함) 또는 첫 태그 앞 글은 원문 그대로 나레이터로 둔다.
            blocks.append(["narrator", None, [line]])
        else:
            blocks[-1][2].append(line)
    parts = []
    for kind, name, lines in blocks:
        body = "\n".join(lines).strip()
        if body:
            parts.append(ParsedPart(speaker_kind=kind, character_name=name, body=body))
    return parts
