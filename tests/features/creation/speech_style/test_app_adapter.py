"""CREATE-US-02 실제 앱 소스 어댑터와 FastAPI·격리 DB의 연결 검사."""

import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import psycopg
import pytest
import uvicorn

from tests.features.creation.images.helpers import give_face_images


@pytest.mark.app_integration
def test_actual_app_adapter_round_trip(adapter_context):
    # 명시한 앱 체크아웃에서만 실행한다. 공유 DB 주소는 읽지 않는다.
    app_root = os.environ.get("CHARCHAT_TEST_APP_ROOT")
    if not app_root:
        pytest.skip("CHARCHAT_TEST_APP_ROOT를 지정하면 앱-API 연결을 검사합니다.")
    script = Path(app_root).resolve() / "scripts/test-expression-server.cjs"
    if not script.is_file() or not shutil.which("node"):
        pytest.fail("앱 검사 소스와 Node가 필요합니다.")
    app, story, dsn = adapter_context
    with psycopg.connect(dsn) as connection:
        connection.execute(
            "UPDATE stories SET subtitle='합동 검사 소개' WHERE id=%s", (story["id"],)
        )
        connection.execute(
            """INSERT INTO characters(story_id,name,description,position)
               VALUES (%s,'가상 캐릭터','가상 상세',0)""",
            (story["id"],),
        )
        give_face_images(connection, story["id"])
        image_id = connection.execute(
            """SELECT ci.image_id FROM character_images ci
               JOIN characters c ON c.id = ci.character_id WHERE c.story_id = %s""",
            (story["id"],),
        ).fetchone()[0]
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started
        result = subprocess.run(
            ["node", str(script), f"http://127.0.0.1:{port}", str(story["id"]), str(image_id)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert '"actualAppAdapter":true' in result.stdout
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive()
