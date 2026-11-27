"""CREATE-US-08-AC02·AC04 Jev 실제 호출 러너의 부모 쪽 안전장치 오프라인 테스트.

실제 Docker·DB·Jev를 부르지 않는다. 스크립트된 가짜 docker로 argv·환경만 확인한다.
"""

import json
import logging

import pytest

from tests.features.creation.save_visibility import jev_live_runner as runner

ENDPOINT = "npipe:////./pipe/docker_engine"
CID = "a" * 64
KEY = "sk-test-not-a-real-key"


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        assert seconds >= 0
        self.now += seconds


class FakeDocker:
    """`docker_(args, *, timeout, env, stage)`를 흉내 낸다. 호출을 기록하고 각본대로 답한다.

    `recover`는 복구 조회 1회마다 쓸 결과 목록이다(마지막 값을 반복한다).
    `probe_cost`만큼 각 복구 조회가 가짜 시계를 진행시킨다.
    """

    def __init__(
        self,
        *,
        endpoint=ENDPOINT,
        run="ok",
        recover=("match",),
        ready_failures=0,
        clock=None,
        probe_cost=0.0,
        ready_cost=0.0,
    ):
        self.endpoint = endpoint
        self.run = run
        self.recover = list(recover)
        self.ready_failures = ready_failures
        self.clock = clock
        self.probe_cost = probe_cost
        self.ready_cost = ready_cost
        self.calls = []
        self.name = self.nonce = None

    def __call__(self, args, *, timeout, env, stage):
        self.calls.append(
            {"args": list(args), "timeout": timeout, "env": dict(env), "stage": stage}
        )
        if args[0] == "context":
            return self.endpoint + "\n"
        assert args[:2] == ["--host", self.endpoint], args
        cmd = args[2:]
        verb = cmd[0]
        if verb == "run":
            self.name = cmd[cmd.index("--name") + 1]
            self.nonce = cmd[cmd.index("--label") + 1].split("=", 1)[1]
            if self.run == "timeout":
                raise runner.DockerTimeout(stage)
            if self.run == "fail":
                raise runner.Stage(stage)
            return CID + "\n"
        if verb == "ps":  # 시작 실패 뒤 생성 라벨로 복구 조회
            assert timeout > 0
            if self.clock is not None:
                self.clock.now += self.probe_cost
            return self._recover_output(stage)
        if verb == "inspect":
            target = cmd[-1]
            assert target == CID
            if "{{json .Mounts}}" in cmd:
                volume = "b" * 64
                return json.dumps(
                    [{"Type": "volume", "Name": volume, "Destination": runner.PG_DATA_DIR}]
                )
            return f"{CID} /{self.name} {self.nonce}\n"
        if verb == "exec":
            assert cmd[1] == CID
            if "pg_isready" in cmd:
                if self.clock is not None:
                    self.clock.now += self.ready_cost
                if self.ready_failures:
                    self.ready_failures -= 1
                    raise runner.Stage(stage)
            return ""
        if verb == "port":
            return "127.0.0.1:55432\n"
        if verb == "rm":
            return ""
        raise AssertionError(f"unexpected docker call: {cmd}")

    def _recover_output(self, stage):
        outcome = self.recover.pop(0) if len(self.recover) > 1 else self.recover[0]
        match = f"{CID} {self.name} {self.nonce}\n"
        if outcome == "absent":  # 조회 성공, 일치 컨테이너 없음
            return ""
        if outcome == "error":  # 데몬 조회 오류(없음을 확인하지 못함)
            raise runner.Stage(stage)
        if outcome == "wrong_label":
            return f"{CID} {self.name} {'0' * 16}\n"
        if outcome == "wrong_name":
            return f"{CID} {self.name}-x {self.nonce}\n"
        if outcome == "short_id":
            return f"{CID[:12]} {self.name} {self.nonce}\n"
        if outcome == "two":
            return match + f"{'c' * 64} {self.name} {self.nonce}\n"
        assert outcome == "match", outcome
        return match

    def verbs(self):
        return [c["args"][2] if c["args"][0] == "--host" else c["args"][0] for c in self.calls]

    def rm_calls(self):
        return [c["args"] for c in self.calls if "rm" in c["args"][:3]]

    def probes(self):
        return [c for c in self.calls if c["args"][2:3] == ["ps"]]


def passing_child(cmd, *, env, cwd, timeout, secrets_):
    return 0, [runner.status(result="passed")]


def run_parent(fake, environ=None, run_child_=passing_child, clock=None):
    lines = []
    base = {"PATH": "x", runner.ENV_KEY: KEY}
    base.update(environ or {})
    clock = clock or fake.clock or FakeClock()
    code = runner.parent_main(
        base,
        docker_=fake,
        run_child_=run_child_,
        out=lines.append,
        clock=clock,
        sleep=clock.sleep,
    )
    return code, lines


# --- 엔드포인트 고정 -----------------------------------------------------------


def test_every_daemon_call_is_pinned_to_verified_local_endpoint_CREATE_US_08_AC02():
    fake = FakeDocker()
    code, lines = run_parent(fake, {"DOCKER_CONTEXT": "desktop-linux"})

    assert code == 0, lines
    context, *daemon = fake.calls
    assert context["args"] == [
        "context",
        "inspect",
        "--format",
        "{{.Endpoints.docker.Host}}",
        "desktop-linux",
    ]
    assert daemon, "daemon calls expected"
    for call in daemon:
        assert call["args"][:2] == ["--host", ENDPOINT]
    for call in fake.calls:
        assert "DOCKER_HOST" not in call["env"]
        assert "DOCKER_CONTEXT" not in call["env"]
        assert runner.ENV_KEY not in call["env"]
    assert fake.rm_calls() == [["--host", ENDPOINT, "rm", "-f", "-v", CID]]


def test_local_docker_host_is_used_without_context_lookup_CREATE_US_08_AC02():
    fake = FakeDocker()
    code, _ = run_parent(fake, {"DOCKER_HOST": ENDPOINT, "DOCKER_CONTEXT": "other"})

    assert code == 0
    assert "context" not in fake.verbs()
    assert all(c["args"][:2] == ["--host", ENDPOINT] for c in fake.calls)
    assert all("DOCKER_HOST" not in c["env"] for c in fake.calls)


@pytest.mark.parametrize(
    "environ, endpoint",
    [
        ({"DOCKER_HOST": "tcp://10.0.0.5:2376"}, ENDPOINT),
        ({}, "tcp://remote.example:2376"),
        ({}, "ssh://user@remote"),
        ({"DOCKER_CONTEXT": "--host=tcp://x:1"}, ENDPOINT),
    ],
)
def test_remote_or_unsafe_docker_endpoint_is_refused_CREATE_US_08_AC02(environ, endpoint):
    fake = FakeDocker(endpoint=endpoint)
    code, lines = run_parent(fake, environ)

    assert code == 1
    assert lines == [runner.status(result="failed", stage="context")]
    assert all(c["args"][0] == "context" for c in fake.calls)


@pytest.mark.parametrize(
    "endpoint",
    [
        "npipe:////server/pipe/docker_engine",  # 원격 서버의 명명된 파이프
        "npipe:////10.0.0.5/pipe/docker_engine",
        "npipe://server/pipe/docker_engine",
        "npipe:////?/pipe/docker_engine",
        "npipe:////./pipe/",
        "npipe:////./pipe/../docker_engine",
        "npipe:////./pipe/x/../../server/pipe/y",
        "npipe:////./pipe/x\\..\\y",
        "npipe:////./pipe/%2e%2e/x",
        "npipe:////./pipe//server/pipe/x",
        "npipe:////./pipe/docker_engine extra",
        "unix://remote/var/run/docker.sock",
        "unix:///var/run/../../tmp/docker.sock",
        "unix://relative.sock",
    ],
)
@pytest.mark.parametrize("via", ["context", "DOCKER_HOST"])
def test_remote_pipe_or_path_trick_is_refused_CREATE_US_08_AC02(endpoint, via):
    fake = FakeDocker(endpoint=endpoint)
    environ = {"DOCKER_HOST": endpoint} if via == "DOCKER_HOST" else {}
    code, lines = run_parent(fake, environ)

    assert code == 1
    assert lines == [runner.status(result="failed", stage="context")]
    assert all(c["args"][0] == "context" for c in fake.calls)


@pytest.mark.parametrize(
    "endpoint",
    [
        "npipe:////./pipe/docker_engine",
        "NPIPE:////./PIPE/Docker_Engine",
        "npipe:////./pipe/dockerDesktopLinuxEngine",
        "unix:///var/run/docker.sock",
    ],
)
def test_local_pipe_is_accepted_case_insensitively_and_pinned_CREATE_US_08_AC02(endpoint):
    fake = FakeDocker(endpoint=endpoint)
    code, lines = run_parent(fake)

    assert code == 0, lines
    for call in fake.calls[1:]:
        assert call["args"][:2] == ["--host", endpoint]
    assert fake.rm_calls() == [["--host", endpoint, "rm", "-f", "-v", CID]]


def test_docker_refuses_conflicting_env_before_running(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("subprocess must not run")

    monkeypatch.setattr(runner.subprocess, "run", boom)
    for name in ("DOCKER_HOST", "DOCKER_CONTEXT", runner.ENV_KEY):
        with pytest.raises(runner.Stage):
            runner.docker(["version"], timeout=1, env={name: "x"}, stage="s")


def test_docker_env_drops_conflicting_selectors_and_key():
    env = runner.docker_env(
        {"PATH": "p", "DOCKER_HOST": "h", "DOCKER_CONTEXT": "c", runner.ENV_KEY: KEY},
        POSTGRES_PASSWORD="pw",
    )
    assert env == {"PATH": "p", "POSTGRES_PASSWORD": "pw"}


# --- 시작 실패 뒤 복구 ---------------------------------------------------------


def test_start_timeout_recovers_exact_name_and_removes_by_full_id_CREATE_US_08_AC02():
    fake = FakeDocker(run="timeout", recover=["match"])
    code, lines = run_parent(fake)

    assert code == 1
    assert lines == [runner.status(result="failed", stage="start")]
    [probe] = fake.probes()
    assert probe["args"][:2] == ["--host", ENDPOINT]
    assert "-a" in probe["args"] and "--no-trunc" in probe["args"]
    assert f"label={runner.LABEL}={fake.nonce}" in probe["args"]
    assert fake.rm_calls() == [["--host", ENDPOINT, "rm", "-f", "-v", CID]]


def test_start_timeout_reprobes_until_delayed_container_appears_CREATE_US_08_AC02():
    clock = FakeClock()
    fake = FakeDocker(run="timeout", recover=["absent", "error", "absent", "match"], clock=clock)
    code, lines = run_parent(fake)

    assert code == 1
    assert lines == [runner.status(result="failed", stage="start")]
    assert len(fake.probes()) == 4
    assert all(p["args"][:2] == ["--host", ENDPOINT] for p in fake.probes())
    assert fake.rm_calls() == [["--host", ENDPOINT, "rm", "-f", "-v", CID]]
    assert clock.now <= 100.0 + runner.RECOVER_TIMEOUT_SECONDS


@pytest.mark.parametrize("recover", [["absent"], ["error"], ["absent", "error"]])
def test_start_timeout_absence_is_inconclusive_and_bounded_CREATE_US_08_AC02(recover):
    clock = FakeClock()
    fake = FakeDocker(run="timeout", recover=recover, clock=clock, probe_cost=3.0)
    code, lines = run_parent(fake)

    assert code == 1
    assert lines == [runner.status(result="failed", stage="recover_uncertain")]
    assert fake.rm_calls() == []
    probes = fake.probes()
    assert 1 < len(probes) < 100
    assert all(0 < p["timeout"] <= runner.RECOVER_PROBE_SECONDS for p in probes)
    # 마감 뒤 조회하지 않고, 대기도 마감을 넘지 않는다(조회 1회 비용만큼만 넘을 수 있다).
    assert clock.now <= 100.0 + runner.RECOVER_TIMEOUT_SECONDS + 3.0


def test_start_failure_with_confirmed_absence_reports_start_CREATE_US_08_AC02():
    fake = FakeDocker(run="fail", recover=["absent"])
    code, lines = run_parent(fake)

    assert code == 1
    assert lines == [runner.status(result="failed", stage="start")]
    assert len(fake.probes()) == 1
    assert fake.rm_calls() == []


def test_start_failure_retries_inspection_error_before_confirming_CREATE_US_08_AC02():
    fake = FakeDocker(run="fail", recover=["error", "error", "absent"])
    code, lines = run_parent(fake)

    assert code == 1
    assert lines == [runner.status(result="failed", stage="start")]
    assert len(fake.probes()) == 3
    assert fake.rm_calls() == []


def test_start_failure_with_only_inspection_errors_is_uncertain_CREATE_US_08_AC02():
    fake = FakeDocker(run="fail", recover=["error"])
    code, lines = run_parent(fake)

    assert code == 1
    assert lines == [runner.status(result="failed", stage="recover_uncertain")]
    assert fake.rm_calls() == []


@pytest.mark.parametrize("recover", ["wrong_label", "wrong_name", "short_id", "two"])
@pytest.mark.parametrize("run", ["timeout", "fail"])
def test_start_failure_does_not_remove_unverified_container_CREATE_US_08_AC02(run, recover):
    script = ["absent", recover] if run == "timeout" else ["error", recover]
    fake = FakeDocker(run=run, recover=script)
    code, lines = run_parent(fake)

    assert code == 1
    assert lines == [runner.status(result="failed", stage="recover")]
    assert len(fake.probes()) == 2
    assert fake.rm_calls() == []


# --- 준비 확인 마감 ------------------------------------------------------------


def test_ready_success_after_deadline_is_failure_CREATE_US_08_AC02():
    clock = FakeClock()

    def late(args, *, timeout, env, stage):
        clock.now += timeout + 0.1  # 성공했지만 마감을 넘겨서 돌아왔다.
        return ""

    with pytest.raises(runner.Stage):
        runner._wait_ready(
            CID,
            "db",
            docker_=late,
            env={},
            deadline=clock.now + 5.0,
            sleep=clock.sleep,
            clock=clock,
        )


def test_late_ready_success_fails_parent_and_still_cleans_up_CREATE_US_08_AC02():
    clock = FakeClock()
    fake = FakeDocker(clock=clock, ready_cost=runner.READY_TIMEOUT_SECONDS + 1)
    code, lines = run_parent(fake)

    assert code == 1
    assert lines == [runner.status(result="failed", stage="ready")]
    assert "psql" not in [a for c in fake.calls for a in c["args"]]
    assert fake.rm_calls() == [["--host", ENDPOINT, "rm", "-f", "-v", CID]]


def test_ready_probe_timeout_is_capped_to_remaining_deadline_CREATE_US_08_AC02():
    clock = FakeClock()
    timeouts = []

    def failing(args, *, timeout, env, stage):
        timeouts.append(timeout)
        clock.now += timeout  # 프로브가 제한 시간을 다 쓴다.
        raise runner.Stage(stage)

    with pytest.raises(runner.Stage):
        runner._wait_ready(
            CID,
            "db",
            docker_=failing,
            env={},
            deadline=clock.now + 12.0,
            sleep=clock.sleep,
            clock=clock,
        )

    assert timeouts[0] == 10
    assert timeouts[1] == pytest.approx(1.5)
    assert all(t > 0 for t in timeouts)
    assert clock.now <= 100.0 + 12.0 + 1e-9


def test_ready_does_not_probe_after_deadline():
    clock = FakeClock()

    def never(args, **kwargs):
        raise AssertionError("must not probe")

    with pytest.raises(runner.Stage):
        runner._wait_ready(
            CID, "db", docker_=never, env={}, deadline=clock.now, sleep=clock.sleep, clock=clock
        )


def test_ready_retries_until_success_CREATE_US_08_AC02():
    fake = FakeDocker(ready_failures=2)
    code, _ = run_parent(fake)

    assert code == 0
    assert sum("pg_isready" in c["args"] for c in fake.calls) == 3


# --- 기존 가드 유지 ------------------------------------------------------------


def test_child_timeout_reports_failure_and_still_cleans_up():
    fake = FakeDocker()

    def hung(cmd, *, env, cwd, timeout, secrets_):
        assert timeout == runner.CHILD_TIMEOUT_SECONDS
        return None, []

    code, lines = run_parent(fake, run_child_=hung)

    assert code == 1
    assert runner.status(result="failed", stage="timeout") in lines
    assert fake.rm_calls() == [["--host", ENDPOINT, "rm", "-f", "-v", CID]]


@pytest.mark.parametrize(
    "dsn",
    [
        "host=localhost port=5432 dbname=x",
        "host=127.0.0.1 hostaddr=10.0.0.1 port=5432",
        "host=127.0.0.1,10.0.0.1 port=5432",
        "host=127.0.0.1 port=5432 service=x",
    ],
)
def test_non_loopback_dsn_is_refused(dsn):
    with pytest.raises(runner.Refused):
        runner.check_loopback_dsn(dsn)


# --- 스토리 공통 안전 모드(--story-safety) ---------------------------------------


def _recording_child(seen):
    def child(cmd, *, env, cwd, timeout, secrets_):
        seen.append({"cmd": list(cmd), "env": dict(env), "timeout": timeout, "secrets": secrets_})
        return 0, [runner.status(result="passed")]

    return child


@pytest.mark.parametrize(
    ("mode", "child_args"),
    [
        (runner.MODE_RATING, ["--child"]),  # 기본 모드는 그대로다.
        (runner.MODE_STORY_SAFETY, ["--child", "--story-safety"]),
        (runner.MODE_SESSION_SAFETY, ["--child", "--session-safety"]),
    ],
)
def test_parent_forwards_single_mode_to_fixed_child_module_CREATE_US_08_AC15(mode, child_args):
    seen = []
    fake = FakeDocker()
    lines = []
    clock = FakeClock()
    code = runner.parent_main(
        {"PATH": "x", runner.ENV_KEY: KEY},
        mode=mode,
        docker_=fake,
        run_child_=_recording_child(seen),
        out=lines.append,
        clock=clock,
        sleep=clock.sleep,
    )
    assert code == 0, lines
    [call] = seen
    assert call["cmd"][1:3] == ["-m", runner.MODULE]
    assert call["cmd"][3:] == child_args
    assert call["timeout"] == runner.CHILD_TIMEOUT_SECONDS
    # DSN·자격 증명은 자식 환경에만, 그리고 정화 대상 비밀 값으로 넘긴다.
    assert set(call["env"]) <= set(runner._PASS_ENV) | {
        "PYTHONPATH",
        "PYTHONIOENCODING",
        "PYTHONDONTWRITEBYTECODE",
        runner.ENV_DSN,
        runner.ENV_DBNAME,
        runner.ENV_MARKER,
        runner.ENV_KEY,
    }
    assert "DATABASE_URL" not in call["env"] and "TEST_DATABASE_URL" not in call["env"]
    assert KEY in call["secrets"] and call["env"][runner.ENV_DSN] in call["secrets"]
    assert all(runner.ENV_KEY not in c["env"] for c in fake.calls)
    assert fake.rm_calls() == [["--host", ENDPOINT, "rm", "-f", "-v", CID]]


def test_parent_rejects_unknown_mode_before_docker_CREATE_US_08_AC15():
    fake = FakeDocker()
    lines = []
    code = runner.parent_main(
        {"PATH": "x", runner.ENV_KEY: KEY}, mode="other", docker_=fake, out=lines.append
    )
    assert code == 2
    assert lines == [runner.status(result="failed", reason="usage")]
    assert fake.calls == []


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([], ("parent", runner.MODE_RATING)),
        (["--story-safety"], ("parent", runner.MODE_STORY_SAFETY)),
        (["--child"], ("child", runner.MODE_RATING)),
        (["--child", "--story-safety"], ("child", runner.MODE_STORY_SAFETY)),
        (["--session-safety"], ("parent", runner.MODE_SESSION_SAFETY)),
        (["--child", "--session-safety"], ("child", runner.MODE_SESSION_SAFETY)),
    ],
)
def test_main_dispatches_single_explicit_mode_CREATE_US_08_AC15(monkeypatch, argv, expected):
    calls = []
    monkeypatch.setattr(runner, "parent_main", lambda *, mode: calls.append(("parent", mode)) or 0)
    monkeypatch.setattr(runner, "child_main", lambda *, mode: calls.append(("child", mode)) or 0)
    assert runner.main(argv) == 0
    assert calls == [expected]


@pytest.mark.parametrize(
    "argv",
    [
        ["--story-safety", "--child"],
        ["--story-safety", "--story-safety"],
        ["--child", "--story-safety", "x"],
        ["--other"],
        ["--session-safety", "--child"],
        ["--session-safety", "--story-safety"],
        ["--story-safety", "--session-safety"],
        ["--child", "--story-safety", "--session-safety"],
        ["--session-safety=1"],
    ],
)
def test_main_rejects_other_argv_CREATE_US_08_AC15(monkeypatch, capsys, argv):
    monkeypatch.setattr(runner, "parent_main", lambda **_: pytest.fail("must not run"))
    monkeypatch.setattr(runner, "child_main", lambda **_: pytest.fail("must not run"))
    assert runner.main(argv) == 2
    assert capsys.readouterr().out.strip() == runner.status(result="failed", reason="usage")


def test_story_safety_evidence_is_allowlisted_status_only_CREATE_US_08_AC15():
    record = {
        "posts": 1,
        "status": 200,
        "elapsed_ms": 812,
        "transport": "ok",
        "outcome": "view_200",
        "body": "raw provider text",  # 허용 목록 밖 값은 싣지 않는다.
    }
    line = runner.evidence(record)
    assert line == runner.status(
        posts=1, status=200, elapsed_ms=812, transport="ok", outcome="view_200"
    )
    assert runner.sanitize(line) == [line]
    assert "raw" not in line


def test_story_safety_evidence_with_missing_values_stays_valid_CREATE_US_08_AC15():
    line = runner.evidence({"posts": 0, "status": None, "elapsed_ms": None, "transport": None})
    assert runner.sanitize(line) == [line]


def test_story_safety_evidence_with_unsafe_value_is_dropped_by_sanitize_CREATE_US_08_AC15():
    line = runner.evidence({"posts": 1, "outcome": "view 200 secret"})
    assert runner.sanitize(line) == []


def test_story_safety_child_refuses_non_loopback_dsn_before_connecting_CREATE_US_08_AC15():
    lines = []
    environ = {
        runner.ENV_DSN: "host=10.0.0.5 port=5432 dbname=jev_smoke_0000000000000000_test",
        runner.ENV_DBNAME: "jev_smoke_0000000000000000_test",
        runner.ENV_MARKER: "m",
        runner.ENV_KEY: KEY,
    }
    try:
        assert runner.child_main(environ, mode=runner.MODE_STORY_SAFETY, out=lines.append) == 1
    finally:
        logging.disable(logging.NOTSET)  # 자식 진입점이 끈 로깅을 테스트 프로세스에서 되돌린다.
    assert lines == [runner.status(result="failed", stage="dsn")]


# --- 자식: 확인한 같은 연결 객체 재사용 -----------------------------------------


_LOOPBACK_DSN = "host=127.0.0.1 port=5432 dbname=jev_smoke_0000000000000000_test"


class FakeConnection:
    """`psycopg.connect(...)` 결과를 흉내 낸다. 실행한 SQL과 열림 상태만 기록한다."""

    def __init__(self):
        self.row_factory = None
        self.closed = False
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def execute(self, query, params=None):
        assert not self.closed
        self.executed.append(query)


def _run_child_offline(monkeypatch, mode):
    """DB·네트워크 없이 자식을 돌리고 (연결 목록, 확인 시점 기록, 스모크 시점 기록)을 돌려준다."""
    import psycopg
    from psycopg.rows import dict_row

    from app.features.creation.save_visibility import jev_http_transport

    connections, verified, smoked = [], [], []

    def connect(dsn, **kwargs):
        assert dsn == _LOOPBACK_DSN and kwargs == {"autocommit": True}
        connections.append(FakeConnection())
        return connections[-1]

    def verify(connection, dbname, marker):
        verified.append((connection, connection.row_factory, list(connection.executed)))

    def loader():
        def run(target, transport, out):
            row_factory = getattr(target, "row_factory", None)
            smoked.append((target, row_factory is dict_row, connections[-1].closed))
            return lambda: None

        return run

    class Transport:
        def __init__(self, key):
            assert key == KEY

    monkeypatch.setattr(psycopg, "connect", connect)
    monkeypatch.setattr(runner, "verify_target", verify)
    monkeypatch.setitem(runner._SMOKES, mode, loader)
    monkeypatch.setattr(jev_http_transport, "JevHttpTransport", Transport)
    environ = {
        runner.ENV_DSN: _LOOPBACK_DSN,
        runner.ENV_DBNAME: "jev_smoke_0000000000000000_test",
        runner.ENV_MARKER: "m",
        runner.ENV_KEY: KEY,
    }
    lines = []
    try:
        code = runner.child_main(environ, mode=mode, out=lines.append)
    finally:
        logging.disable(logging.NOTSET)
    assert (code, lines) == (0, [runner.status(result="passed")])
    return connections, verified, smoked


@pytest.mark.parametrize("mode", [runner.MODE_STORY_SAFETY, runner.MODE_SESSION_SAFETY])
def test_story_child_reuses_verified_connection_for_schema_seed_and_app_CREATE_US_08_AC15(
    monkeypatch, mode
):
    connections, verified, smoked = _run_child_offline(monkeypatch, mode)
    [connection] = connections  # 연결은 하나뿐이다(DSN으로 다시 연결하지 않는다).
    [(checked, factory_at_verify, executed_before)] = verified
    assert checked is connection
    assert factory_at_verify is None and executed_before == []  # 확인 전에는 쓰지 않는다.
    assert connection.executed == [runner.SCHEMA_SQL.read_text(encoding="utf-8")]
    # 스모크(시드·앱 요청 커넥션)는 확인한 그 연결 객체를, 확인 뒤 dict_row로, 열린 채로 받는다.
    assert smoked == [(connection, True, False)]
    assert connection.closed


def test_rating_child_keeps_dsn_based_smoke_unchanged_CREATE_US_08_AC02(monkeypatch):
    connections, verified, smoked = _run_child_offline(monkeypatch, runner.MODE_RATING)
    [connection] = connections
    assert verified[0][0] is connection
    assert connection.row_factory is None
    # 기본 모드는 기존대로 스키마 연결을 닫은 뒤 DSN 문자열로 스모크한다.
    assert smoked == [(_LOOPBACK_DSN, False, True)]
    assert connection.closed


# --- 세션 안전 모드(--session-safety) -------------------------------------------------


def test_session_safety_mode_is_routed_only_by_fixed_argv_CREATE_US_08_AC15():
    """모드는 고정 argv로만 정해진다(설정·환경변수로 켜지 않는다)."""
    assert runner._PARENT_ARGV[("--session-safety",)] == runner.MODE_SESSION_SAFETY
    assert runner._CHILD_ARGS[runner.MODE_SESSION_SAFETY] == ("--child", "--session-safety")
    assert runner._SMOKES[runner.MODE_SESSION_SAFETY] is runner._session_safety_smoke


def test_session_safety_child_refuses_non_loopback_dsn_before_connecting_CREATE_US_08_AC15():
    lines = []
    environ = {
        runner.ENV_DSN: "host=10.0.0.5 port=5432 dbname=jev_smoke_0000000000000000_test",
        runner.ENV_DBNAME: "jev_smoke_0000000000000000_test",
        runner.ENV_MARKER: "m",
        runner.ENV_KEY: KEY,
    }
    try:
        assert runner.child_main(environ, mode=runner.MODE_SESSION_SAFETY, out=lines.append) == 1
    finally:
        logging.disable(logging.NOTSET)
    assert lines == [runner.status(result="failed", stage="dsn")]


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (
            (
                {
                    "posts": 1,
                    "status": 200,
                    "elapsed_ms": 5,
                    "transport": "ok",
                    "outcome": "start_201",
                },
                201,
                1,
                True,
            ),
            0,
        ),
        (
            (
                {
                    "posts": 1,
                    "status": 200,
                    "elapsed_ms": 5,
                    "transport": "ok",
                    "outcome": "start_201",
                },
                201,
                2,
                True,
            ),
            1,
        ),  # 두 번째 외부 요청 시도
        (
            (
                {
                    "posts": 1,
                    "status": 200,
                    "elapsed_ms": 5,
                    "transport": "ok",
                    "outcome": "start_201",
                },
                201,
                1,
                False,
            ),
            1,
        ),  # 저장 스냅숏 불일치
        (
            (
                {
                    "posts": 1,
                    "status": 200,
                    "elapsed_ms": 5,
                    "transport": "ok",
                    "outcome": "start_404",
                },
                404,
                1,
                False,
            ),
            1,
        ),
    ],
)
def test_session_safety_smoke_loader_reports_evidence_and_asserts_CREATE_US_08_AC15(
    monkeypatch, result, expected
):
    from tests.features.creation.save_visibility import jev_session_safety_live as live

    seen = []
    monkeypatch.setattr(
        live, "run_start_smoke", lambda connection, transport: seen.append(connection) or result
    )
    lines = []
    marker = object()
    check = runner._session_safety_smoke()(marker, object(), lines.append)
    assert seen == [marker]
    assert lines == [runner.evidence(result[0])]
    assert runner.sanitize(lines[0]) == lines
    if expected == 0:
        check()
    else:
        with pytest.raises((AssertionError, live.PersistMismatch)):
            check()
