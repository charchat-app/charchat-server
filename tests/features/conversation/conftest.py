"""대화 에픽 테스트 공통 준비.

CHAT-US-08: 응답 뒤 장면 판단 호출은 기본적으로 '바뀐 것 없음'만 돌려주는 가짜로 둔다. 그래야 다른
스토리의 테스트가 기록하는 모델 요청 수·내용이 판단 호출 때문에 달라지지 않는다. 장면 테스트는
`scene_judge` 픽스처로 판단 결과를 정한다.
테스트 안의 monkeypatch.undo()에 풀리지 않도록 직접 바꾸고 되돌린다.
"""

import pytest

from app.features.conversation.scene import judge as scene_judge


class NoChangeJudge:
    def __init__(self):
        self.requests = []

    def stream(self, request):
        self.requests.append(request)
        yield '{"changed": false}'


@pytest.fixture(autouse=True)
def no_scene_change():
    original = scene_judge.get_judge_provider
    provider = NoChangeJudge()
    scene_judge.get_judge_provider = lambda: provider
    yield provider
    scene_judge.get_judge_provider = original
