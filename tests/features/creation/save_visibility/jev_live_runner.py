"""CREATE-US-08-AC02·AC04 Jev 실제 호출 개발자 스모크 러너.

pytest 수집 대상이 아니며 CI에서 실행하지 않는다.
사용: `python -m tests.features.creation.save_visibility.jev_live_runner [--story-safety]`
- 인자 없음(기본): CREATE-US-08-AC02·AC04 등급 검토 스모크(기존 동작 그대로).
- `--story-safety`: CREATE-US-08-AC15 스토리 공통 안전 후보의 관람 미리보기 1회 스모크.
  같은 고정 자식 모듈·일회용 DB·DSN 격리·정리를 그대로 쓰고, 자식에 모드 하나만 넘긴다.
  자식은 대상 확인을 마친 연결 객체 하나로 스키마·시드·앱 요청 커넥션을 모두 처리하고,
  보내기 전에 요청 전체를 고정 기대 요청과 비교한다.
실행 셸에 `OPENROUTER_API_KEY`를 명시적으로 넣는다(`.env`를 읽지 않는다).

부모(`app`을 import하지 않음)
- 로컬 Docker 엔진(`npipe:////./pipe/<이름>`·절대 경로 unix 소켓)만 쓴다.
  원격 파이프·경로 우회·원격 컨텍스트·`DOCKER_HOST`는 거부한다.
  확인한 엔드포인트 하나를 모든 데몬 호출 argv에 `--host`로 고정하고,
  Docker CLI 환경에서 `DOCKER_HOST`·`DOCKER_CONTEXT`를 뺀다.
- `docker run`이 실패·시간 초과하면 고정 데몬에서 nonce 라벨로 정해진 시간 안에 다시 조회해
  전체 ID·생성 이름·라벨이 모두 맞을 때만 그 ID로 지운다. 시간 초과 뒤 끝내 못 찾았거나
  조회가 계속 실패하면 없음을 확인하지 못한 것으로 `recover_uncertain`을 알린다.
- 새 일회용 PostgreSQL 컨테이너를 만든다. 이름·DB·비밀번호는 무작위이고,
  포트는 `127.0.0.1` 임의 포트다. 볼륨·바인드 마운트를 주지 않는다.
- 마운트가 postgres 익명 데이터 볼륨 하나뿐인지 확인한다.
- Docker CLI 환경에는 API 키를 넣지 않는다.
- DB 주석에 일회용 표식을 남기고 자식을 하드 타임아웃으로 실행한다.
- 성공·실패와 관계없이 `docker run`이 돌려주거나 복구 조회로 확인한 전체 ID로만 지운다.
- 준비 확인 프로브·대기는 남은 전체 마감 안으로 줄인다.

자식
- 연결 전에 DSN이 `127.0.0.1` 단일 호스트인지 확인한다.
- 쓰기 전에 DB 이름 형식, 이름·표식 일치, 빈 public 스키마, 기본 외 DB 없음을 확인한다.
  하나라도 다르면 아무것도 쓰지 않고 멈춘다.
- 빈 임시 폴더에서 최소 환경변수로 실행한다.
  `.env`·`DATABASE_URL`·`TEST_DATABASE_URL`을 보지 않는다.

출력
- 부모는 자식 stderr를 읽지 않고 버린다.
- stdout에서 허용 목록 형식의 `JEV_LIVE_SMOKE` 줄만, 비밀 값이 없을 때 전달한다.
- 키·DSN·비밀번호·트레이스백은 출력하지 않는다.
"""

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from psycopg.conninfo import conninfo_to_dict, make_conninfo

ROOT = Path(__file__).resolve().parents[4]
SCHEMA_SQL = ROOT / "app" / "db" / "schema.sql"
IMAGE = "postgres:17"  # docker-compose.yml과 같은 이미지
PG_DATA_DIR = "/var/lib/postgresql/data"  # postgres:17 이미지의 VOLUME
PREFIX = "JEV_LIVE_SMOKE"
MODULE = "tests.features.creation.save_visibility.jev_live_runner"
LABEL = "charchat.jev-smoke"
LOOPBACK = "127.0.0.1"
CHILD_TIMEOUT_SECONDS = 180.0  # 자식 전체 하드 마감(연결·DNS·Jev·DB 확인)
READY_TIMEOUT_SECONDS = 60.0
READY_PROBE_SECONDS = 10.0  # 준비 확인 1회 상한(남은 전체 마감보다 길지 않게 줄인다)
DOCKER_TIMEOUT_SECONDS = 300.0  # 이미지 내려받기 포함
DBNAME_RE = re.compile(r"jev_smoke_[0-9a-f]{16}_test")
CONTAINER_ID_RE = re.compile(r"[0-9a-f]{64}")
RECOVER_TIMEOUT_SECONDS = 30.0  # 시작 실패 뒤 복구 재조회 전체 상한
RECOVER_PROBE_SECONDS = 10.0
RECOVER_INTERVAL_SECONDS = 1.0
# 로컬 엔드포인트만: 이 기기의 명명된 파이프(`//./pipe/<이름>`) 또는 절대 경로 unix 소켓.
# 원격 파이프(`//server/pipe/...`)·경로 이동(`..`·`\`·`%`)·공백은 맞지 않는다.
_SAFE_SEGMENT = r"(?!\.\.?(?:/|$))[A-Za-z0-9_.\-]+"
_LOCAL_DOCKER_RE = re.compile(
    rf"npipe:////\./pipe/{_SAFE_SEGMENT}|unix:///{_SAFE_SEGMENT}(?:/{_SAFE_SEGMENT})*",
    re.IGNORECASE,
)
_CONTEXT_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+\-]*")  # Docker 컨텍스트 이름 규칙
# 데몬 선택을 바꾸는 변수. Docker CLI 환경에서 뺀다(엔드포인트는 argv `--host`로 고정).
_DOCKER_SELECTORS = ("DOCKER_HOST", "DOCKER_CONTEXT")
IDENTITY_FMT = f'{{{{.Id}}}} {{{{.Name}}}} {{{{index .Config.Labels "{LABEL}"}}}}'
PS_IDENTITY_FMT = f'{{{{.ID}}}} {{{{.Names}}}} {{{{.Label "{LABEL}"}}}}'  # `ps`는 이름에 `/`가 없다
_ALLOWED_KEYS = frozenset(
    {"posts", "status", "elapsed_ms", "transport", "outcome", "result", "stage", "reason"}
)
_LINE_RE = re.compile(rf"{PREFIX}( [a-z_]+=[A-Za-z0-9_:.\-]{{1,40}}){{1,8}}")
_MAX_FORWARDED = 8
# 자식에 넘기는 환경변수(그 밖은 넘기지 않는다).
# Windows 소켓·DNS에 SYSTEMROOT가 필요하다.
_PASS_ENV = ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "PATH", "TEMP", "TMP", "HOME", "LANG")
ENV_DSN = "CHARCHAT_JEV_SMOKE_DSN"
ENV_DBNAME = "CHARCHAT_JEV_SMOKE_DBNAME"
ENV_MARKER = "CHARCHAT_JEV_SMOKE_MARKER"
ENV_KEY = "OPENROUTER_API_KEY"
MODE_RATING = "rating"
MODE_STORY_SAFETY = "story_safety"
# 모드마다 고정 자식 argv(모듈은 `MODULE` 하나로 고정).
_CHILD_ARGS = {MODE_RATING: ("--child",), MODE_STORY_SAFETY: ("--child", "--story-safety")}
_PARENT_ARGV = {(): MODE_RATING, ("--story-safety",): MODE_STORY_SAFETY}
_CHILD_ARGV = {args: mode for mode, args in _CHILD_ARGS.items()}
# 스토리 공통 안전 모드의 증거 줄에 싣는 기록 키(원문·응답은 싣지 않는다).
EVIDENCE_KEYS = ("posts", "status", "elapsed_ms", "transport", "outcome")


class Refused(Exception):
    """대상이 이 러너가 만든 새 일회용 DB임을 확인하지 못했다(메시지에 값 없음)."""


class Stage(Exception):
    """부모 단계 실패. 단계 이름만 담는다."""


class DockerTimeout(Stage):
    """docker CLI가 시간 안에 끝나지 않았다(데몬 쪽 작업 결과를 모른다)."""


def status(**fields):
    return f"{PREFIX} " + " ".join(f"{k}={v}" for k, v in fields.items())


def evidence(record):
    """기록에서 허용 키만 골라 `status` 한 줄로 만든다. 값 형식은 `sanitize`가 다시 거른다."""
    return status(**{key: record[key] for key in EVIDENCE_KEYS if key in record})


# --- 출력 정화 -----------------------------------------------------------------


def sanitize(text, secrets_=()):
    """허용 목록 형식·키의 `JEV_LIVE_SMOKE` 줄만, 비밀 값이 없을 때 돌려준다."""
    kept = []
    for line in text.splitlines():
        line = line.strip()
        if not _LINE_RE.fullmatch(line):
            continue
        keys = [pair.split("=", 1)[0] for pair in line.split(" ")[1:]]
        if any(key not in _ALLOWED_KEYS for key in keys):
            continue
        if any(s and s in line for s in secrets_):
            continue
        kept.append(line)
        if len(kept) >= _MAX_FORWARDED:
            break
    return kept


# --- 자식 실행(하드 타임아웃) --------------------------------------------------


def run_child(cmd, env, cwd, timeout, secrets_=()):
    """자식을 실행하고 (종료 코드 또는 시간 초과 시 None, 정화된 줄)을 돌려준다.

    stderr는 읽지 않고 버린다. 시간을 넘기면 자식을 강제 종료한다
    (Windows TerminateProcess). 끝나지 않는 DNS 조회 스레드도 함께 끝난다.
    """
    proc = subprocess.Popen(
        cmd,
        env=env,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        return None, []
    return proc.returncode, sanitize(out.decode("utf-8", "replace"), secrets_)


def child_env(base, *, dsn, dbname, marker, key):
    env = {name: base[name] for name in _PASS_ENV if name in base}
    env.update(
        {
            "PYTHONPATH": str(ROOT),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            ENV_DSN: dsn,
            ENV_DBNAME: dbname,
            ENV_MARKER: marker,
            ENV_KEY: key,
        }
    )
    return env


# --- 부모: 새 일회용 컨테이너 --------------------------------------------------


def docker_env(base, **extra):
    """Docker CLI 환경. API 키와 데몬 선택 변수(`DOCKER_HOST`·`DOCKER_CONTEXT`)는 뺀다."""
    env = {k: v for k, v in base.items() if k != ENV_KEY and k not in _DOCKER_SELECTORS}
    env.update(extra)
    return env


def docker(args, *, timeout, env, stage):
    """docker CLI를 부른다. 출력은 호출자에게만 돌려주고 화면에 내보내지 않는다."""
    if ENV_KEY in env or any(name in env for name in _DOCKER_SELECTORS):
        # 키가 Docker 쪽으로 새거나 argv 밖의 변수가 데몬을 바꾸지 않게 막는다.
        raise Stage(stage)
    try:
        done = subprocess.run(
            ["docker", *args],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise DockerTimeout(stage) from None
    except OSError:
        raise Stage(stage) from None
    if done.returncode != 0:
        raise Stage(stage)
    return done.stdout.decode("utf-8", "replace")


def check_local_docker(environ, *, docker_, env):
    """원격 Docker를 거부하고, 확인한 로컬 엔드포인트를 돌려준다.

    Docker CLI와 같은 우선순위로 `DOCKER_HOST`, `DOCKER_CONTEXT`, 현재 컨텍스트 순으로 본다.
    컨텍스트 이름은 argv로 넘기고, `env`(데몬 선택 변수를 뺀 환경)로 조회한다.
    """
    host = environ.get("DOCKER_HOST") or None
    if host is None:
        args = ["context", "inspect", "--format", "{{.Endpoints.docker.Host}}"]
        context = environ.get("DOCKER_CONTEXT")
        if context:
            if not _CONTEXT_NAME_RE.fullmatch(context):
                raise Stage("context")
            args.append(context)
        host = docker_(args, timeout=30, env=env, stage="context").strip()
    if not _LOCAL_DOCKER_RE.fullmatch(host):
        raise Stage("context")
    return host


def pinned(docker_, host):
    """모든 데몬 호출 argv 앞에 확인한 엔드포인트를 `--host`로 붙인다."""

    def call(args, **kwargs):
        return docker_(["--host", host, *args], **kwargs)

    return call


def _match_started(raw, name, nonce):
    """복구 조회 결과에서 None(확인된 없음) 또는 일치하는 전체 ID를 돌려준다.

    결과가 있는데 정확히 한 개의 전체 ID·생성 이름·nonce 라벨이 아니면
    지우지 않도록 `Stage("recover")`를 낸다.
    """
    rows = [row.strip() for row in raw.splitlines() if row.strip()]
    if not rows:
        return None
    if len(rows) == 1:
        parts = rows[0].split(" ")
        if len(parts) == 3 and CONTAINER_ID_RE.fullmatch(parts[0]) and parts[1:] == [name, nonce]:
            return parts[0]
    raise Stage("recover")


def recover_started(
    name, nonce, *, docker_, env, timed_out, clock=time.monotonic, sleep=time.sleep
):
    """`docker run` 실패 뒤 고정 데몬에서 생성 라벨(nonce)로 컨테이너를 다시 찾는다.

    - 전체 ID·정확한 생성 이름·nonce가 모두 맞는 한 개를 찾으면 그 전체 ID를 돌려준다.
    - 결과가 그와 다르면 `Stage("recover")`(지우지 않음).
    - `docker run`이 시간 초과가 아닌 실패이면 조회 성공의 빈 결과를 확인된 없음(None)으로 본다.
    - 시간 초과였으면 컨테이너가 늦게 생길 수 있어, 빈 결과는 결론이 아니다.
      상한 안에서 다시 조회하고 끝내 못 찾으면 `Stage("recover_uncertain")`을 낸다.
    - 조회 오류는 없음의 확인이 아니다. 상한 안에서 다시 조회한다.
    조회·대기는 남은 복구 상한을 넘지 않는다.
    """
    deadline = clock() + RECOVER_TIMEOUT_SECONDS
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            raise Stage("recover_uncertain")
        try:
            raw = docker_(
                [
                    "ps",
                    "-a",
                    "--no-trunc",
                    "--filter",
                    f"label={LABEL}={nonce}",
                    "--format",
                    PS_IDENTITY_FMT,
                ],
                timeout=min(RECOVER_PROBE_SECONDS, remaining),
                env=env,
                stage="recover_uncertain",
            )
        except Stage:
            raw = None  # 조회 오류: 없음을 확인하지 못했다.
        if raw is not None:
            cid = _match_started(raw, name, nonce)
            if cid is not None:
                return cid
            if not timed_out:
                return None
        sleep(max(0.0, min(RECOVER_INTERVAL_SECONDS, deadline - clock())))


def check_mounts(raw):
    """마운트가 postgres 데이터 경로의 익명 볼륨 하나뿐인지 확인한다."""
    try:
        mounts = json.loads(raw)
    except ValueError:
        raise Stage("mounts") from None
    if not isinstance(mounts, list) or len(mounts) != 1:
        raise Stage("mounts")
    [mount] = mounts
    if not (
        isinstance(mount, dict)
        and mount.get("Type") == "volume"
        and mount.get("Destination") == PG_DATA_DIR
        and CONTAINER_ID_RE.fullmatch(str(mount.get("Name", "")))  # 익명 볼륨은 64자 hex
    ):
        raise Stage("mounts")


def check_identity(raw, cid, name, nonce):
    """`docker inspect` 결과가 정확히 이 러너가 만든 컨테이너인지 확인한다."""
    if raw.strip() != f"{cid} /{name} {nonce}":
        raise Stage("identity")


def check_loopback_dsn(dsn):
    """DSN이 `127.0.0.1` 단일 호스트를 가리키는지 연결 전에 확인한다."""
    try:
        params = conninfo_to_dict(dsn)
    except Exception:
        raise Refused from None
    if params.get("host") != LOOPBACK:
        raise Refused
    if params.get("hostaddr") not in (None, LOOPBACK):
        raise Refused
    if not str(params.get("port", "")).isdigit():
        raise Refused
    if "service" in params or "passfile" in params:
        raise Refused


def _wait_ready(name, dbname, *, docker_, env, deadline, sleep=time.sleep, clock=time.monotonic):
    # 초기화용 임시 서버는 TCP를 열지 않으므로 TCP로 확인해야 실제 준비를 본다.
    # 각 프로브와 대기는 남은 전체 마감을 넘지 않는다.
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            raise Stage("ready")
        try:
            docker_(
                [
                    "exec",
                    name,
                    "pg_isready",
                    "-q",
                    "-h",
                    LOOPBACK,
                    "-U",
                    "postgres",
                    "-d",
                    dbname,
                ],
                timeout=min(READY_PROBE_SECONDS, remaining),
                env=env,
                stage="ready",
            )
        except Stage:
            sleep(max(0.0, min(0.5, deadline - clock())))
            continue
        if clock() >= deadline:  # 마감을 넘겨 돌아온 성공은 준비로 보지 않는다.
            raise Stage("ready")
        return


def _host_port(output):
    for line in output.splitlines():
        match = re.fullmatch(r"127\.0\.0\.1:(\d{1,5})", line.strip())
        if match:
            return int(match.group(1))
    raise Stage("port")


def _inspect(cid, fmt, *, docker_, env, stage):
    return docker_(["inspect", "--format", fmt, cid], timeout=30, env=env, stage=stage)


def parent_main(
    environ=None,
    *,
    mode=MODE_RATING,
    docker_=docker,
    run_child_=run_child,
    out=print,
    clock=time.monotonic,
    sleep=time.sleep,
):
    if mode not in _CHILD_ARGS:
        out(status(result="failed", reason="usage"))
        return 2
    environ = os.environ if environ is None else environ
    key = environ.get(ENV_KEY)
    if not key:
        out(status(result="skipped", reason="no_key"))
        return 2
    if docker_ is docker and shutil.which("docker") is None:
        out(status(result="skipped", reason="no_docker"))
        return 2

    nonce = secrets.token_hex(8)
    name = f"charchat-jev-smoke-{nonce}"
    dbname = f"jev_smoke_{nonce}_test"
    password = secrets.token_urlsafe(24)
    marker = f"charchat-jev-smoke:{secrets.token_hex(16)}"
    denv = docker_env(environ)
    cid = None
    dock = None
    code = 1
    try:
        dock = pinned(docker_, check_local_docker(environ, docker_=docker_, env=denv))
        # 비밀번호는 argv가 아니라 docker CLI 환경변수로 넘긴다(`-e NAME`은 환경에서 읽는다).
        try:
            started = dock(
                [
                    "run",
                    "-d",
                    "--rm",
                    "--name",
                    name,
                    "--label",
                    f"{LABEL}={nonce}",
                    "-e",
                    "POSTGRES_PASSWORD",
                    "-e",
                    f"POSTGRES_DB={dbname}",
                    "-p",
                    f"{LOOPBACK}::5432",
                    IMAGE,
                ],
                timeout=DOCKER_TIMEOUT_SECONDS,
                env=docker_env(environ, POSTGRES_PASSWORD=password),
                stage="start",
            ).strip()
            if not CONTAINER_ID_RE.fullmatch(started):
                raise Stage("start")
        except Stage as failed:
            # 시간 초과·실패여도 컨테이너가 생겼을 수 있다. 확인된 경우에만 지운다.
            cid = recover_started(
                name,
                nonce,
                docker_=dock,
                env=denv,
                timed_out=isinstance(failed, DockerTimeout),
                clock=clock,
                sleep=sleep,
            )
            raise Stage("start") from None
        cid = started
        check_identity(
            _inspect(cid, IDENTITY_FMT, docker_=dock, env=denv, stage="identity"), cid, name, nonce
        )
        check_mounts(_inspect(cid, "{{json .Mounts}}", docker_=dock, env=denv, stage="mounts"))
        _wait_ready(
            cid,
            dbname,
            docker_=dock,
            env=denv,
            deadline=clock() + READY_TIMEOUT_SECONDS,
            sleep=sleep,
            clock=clock,
        )
        dock(
            [
                "exec",
                cid,
                "psql",
                "-q",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                "postgres",
                "-d",
                dbname,
                "-c",
                f"COMMENT ON DATABASE \"{dbname}\" IS '{marker}'",
            ],
            timeout=30,
            env=denv,
            stage="marker",
        )
        port = _host_port(dock(["port", cid, "5432/tcp"], timeout=30, env=denv, stage="port"))
        dsn = make_conninfo(
            host=LOOPBACK,
            port=port,
            dbname=dbname,
            user="postgres",
            password=password,
            connect_timeout=10,
        )
        try:
            check_loopback_dsn(dsn)
        except Refused:
            raise Stage("dsn") from None
        env = child_env(environ, dsn=dsn, dbname=dbname, marker=marker, key=key)
        with tempfile.TemporaryDirectory(prefix="jev-smoke-") as cwd:  # .env 없는 빈 폴더
            returncode, lines = run_child_(
                [sys.executable, "-m", MODULE, *_CHILD_ARGS[mode]],
                env=env,
                cwd=cwd,
                timeout=CHILD_TIMEOUT_SECONDS,
                secrets_=(key, password, dsn, marker),
            )
        for line in lines:
            out(line)
        if returncode is None:
            out(status(result="failed", stage="timeout"))
        elif returncode == 0 and status(result="passed") in lines:
            out(status(result="passed"))
            code = 0
        else:
            out(status(result="failed", stage="child"))
    except Stage as failed:
        out(status(result="failed", stage=failed.args[0]))
    finally:
        if cid is not None:
            # 확인한 전체 ID로만, 같은 고정 엔드포인트에서 지운다(이름·필터로 고르지 않는다).
            try:
                dock(["rm", "-f", "-v", cid], timeout=60, env=denv, stage="cleanup")
            except Stage:
                out(status(result="failed", stage="cleanup"))
                code = 1
    return code


# --- 자식: 대상 확인 → 스키마 → 스모크 ----------------------------------------


def verify_target(connection, dbname, marker):
    """쓰기 전에 대상이 방금 만든 새 일회용 DB인지 읽기만으로 확인한다."""
    if not DBNAME_RE.fullmatch(dbname or "") or not marker:
        raise Refused
    row = connection.execute(
        """SELECT current_database(), shobj_description(oid, 'pg_database')
           FROM pg_database WHERE datname = current_database()"""
    ).fetchone()
    if row is None or tuple(row) != (dbname, marker):
        raise Refused
    [(relations,)] = connection.execute(
        """SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = 'public'"""
    ).fetchall()
    if relations != 0:
        raise Refused
    [(others,)] = connection.execute(
        """SELECT count(*) FROM pg_database
           WHERE datname NOT IN ('postgres', 'template0', 'template1', %s)""",
        (dbname,),
    ).fetchall()
    if others != 0:
        raise Refused


# 모드별 스모크 적재기: import만 하고 `run(target, transport, out) -> check`를 돌려준다.
# 기본(등급) 모드의 `target`은 DSN이고, 스토리 공통 안전 모드는 확인을 마친 연결 객체다.
def _rating_smoke():
    from tests.features.creation.save_visibility.test_jev_live_smoke import (
        assert_approved,
        run_smoke,
    )

    def run(dsn, transport, out):
        result = run_smoke(dsn, transport)
        return lambda: assert_approved(*result)

    return run


def _story_safety_smoke():
    from tests.features.creation.save_visibility.jev_story_safety_live import (
        assert_viewer_clear,
        run_viewer_smoke,
    )

    def run(connection, transport, out):
        record, view_status = run_viewer_smoke(connection, transport)
        out(evidence(record))  # 허용 키 값만. 부모가 `sanitize`로 다시 거른다.
        return lambda: assert_viewer_clear(record, view_status)

    return run


_SMOKES = {MODE_RATING: _rating_smoke, MODE_STORY_SAFETY: _story_safety_smoke}


def child_main(environ=None, *, mode=MODE_RATING, out=print):
    import logging
    from contextlib import ExitStack

    logging.disable(logging.CRITICAL)
    environ = os.environ if environ is None else environ
    stage = "config"
    try:
        dsn, dbname, marker = environ[ENV_DSN], environ[ENV_DBNAME], environ[ENV_MARKER]
        key = environ[ENV_KEY]
        load_smoke = _SMOKES[mode]  # 알 수 없는 모드는 쓰기 전에 멈춘다.

        stage = "dsn"
        check_loopback_dsn(dsn)

        import psycopg
        from psycopg.rows import dict_row

        stage = "verify"
        with ExitStack() as stack:
            connection = stack.enter_context(psycopg.connect(dsn, autocommit=True))
            verify_target(connection, dbname, marker)
            stage = "schema"
            connection.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
            if mode == MODE_RATING:
                stack.close()  # 기본 모드는 기존대로 연결을 닫고 DSN으로 스모크한다.
                target = dsn
            else:
                # 확인한 같은 연결 객체를 시드·앱 요청 커넥션까지 그대로 쓴다(재연결 없음).
                connection.row_factory = dict_row
                target = connection

            stage = "import"
            from tests.features.creation.save_visibility.jev_http_transport import (
                JevHttpTransport,
            )

            smoke = load_smoke()

            stage = "transport"
            transport = JevHttpTransport(key)
            del key
            stage = "smoke"
            check = smoke(target, transport, out)
            stage = "assert"
            check()
    except BaseException as error:  # 원문·트레이스백 없이 단계만 알린다.
        if isinstance(error, KeyboardInterrupt):
            raise
        out(status(result="failed", stage=stage))
        return 1
    out(status(result="passed"))
    return 0


def main(argv=None):
    argv = tuple(sys.argv[1:] if argv is None else argv)
    if argv in _CHILD_ARGV:
        return child_main(mode=_CHILD_ARGV[argv])
    if argv in _PARENT_ARGV:
        return parent_main(mode=_PARENT_ARGV[argv])
    print(status(result="failed", reason="usage"))
    return 2


if __name__ == "__main__":
    sys.exit(main())
