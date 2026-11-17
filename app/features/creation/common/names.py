"""인물 이름 공통 규칙. CREATE-US-01(캐릭터)이 쓰고, CREATE-US-05(유저 프로필)도 같은 규칙을 따른다.

- 앞뒤 띄어쓰기는 형식 오류다. 자동으로 잘라 통과시키지 않는다 (CREATE-US-01-AC11).
- 중복 비교는 영문 대소문자를 구분하지 않고 띄어쓰기를 모두 뺀 값으로 한다 (CREATE-US-01-AC10).
- 저장·표시는 입력한 원래 표기를 그대로 쓴다.
"""

import re

_WHITESPACE = re.compile(r"\s+")


def has_surrounding_space(name: str) -> bool:
    return name != name.strip()


def name_compare_key(name: str) -> str:
    """중복 비교 키. 예: 'Alex'·'alex' → 'alex', '이도형'·'이 도형' → '이도형'."""
    return _WHITESPACE.sub("", name).lower()
