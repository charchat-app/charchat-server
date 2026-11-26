"""BASE-03 로그 마스킹: 로그에 cca_/ccr_ 토큰 원문과 code·state 쿼리 값이 남지 않는다."""

import json
import logging

import jwt
import pytest

from app.features.account.auth.log_masking import (
    SecretMaskingFilter,
    install_log_masking,
    is_log_masking_installed,
    mask_secrets,
)
from tests.features.account.account_helpers import (
    CURRENT_SESSION,
    ME,
    STORIES,
    active_login,
    bearer,
    dev_login,
    expired_access_token,
    password_login,
    refresh,
    signup,
)

TOKEN = "cca_" + "x1-_" * 10 + "abc"
REFRESH = "ccr_" + "Q" * 43


def test_mask_secrets_hides_tokens_and_query_codes_BASE_03():
    text = f"GET /cb?code=SECRET1&state=S2&x=1&login_code=L3 Bearer {TOKEN} {REFRESH}"
    masked = mask_secrets(text)
    for secret in ("SECRET1", "S2", "L3", TOKEN, REFRESH):
        assert secret not in masked
    assert "x=1" in masked
    assert "cca_***" in masked and "ccr_***" in masked


def test_access_log_args_are_masked_after_create_app_BASE_03(client, caplog):
    """create_app이 로그 기록 마스킹을 설치한다. uvicorn 접근 로그는 args로 형식을 만든다."""
    access = logging.getLogger("uvicorn.access")
    assert is_log_masking_installed()
    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        access.info(
            '%s - "%s %s HTTP/%s" %d',
            "127.0.0.1:5000",
            "GET",
            f"/api/v1/auth/providers/kakao/callback?code=AUTHCODE&state=STATE1&t={TOKEN}",
            "1.1",
            302,
        )
    assert "AUTHCODE" not in caplog.text
    assert "STATE1" not in caplog.text
    assert TOKEN not in caplog.text
    assert "callback?code=***&state=***" in caplog.text


def test_auth_flow_logs_contain_no_token_text_BASE_03(client, db, caplog):
    with caplog.at_level(logging.DEBUG):
        first = active_login(client, "alice")
        rotated = refresh(client, first["refresh_token"]).json()
        refresh(client, first["refresh_token"])  # 409 재사용
        client.get(STORIES, headers=bearer(rotated["access_token"]))
        client.get(STORIES, headers=bearer(first["access_token"]))  # 401 INVALID
        expired = expired_access_token(rotated["access_token"])
        client.get(ME, headers=bearer(expired))  # 401 EXPIRED
        second = dev_login(client, "bob")
        client.delete(CURRENT_SESSION, headers=bearer(second["access_token"]))
    tokens = [
        first["access_token"],
        first["refresh_token"],
        rotated["access_token"],
        rotated["refresh_token"],
        second["access_token"],
        second["refresh_token"],
        expired,
    ]
    assert caplog.records, "흐름 중 로그가 하나도 없으면 이 검사가 의미가 없다"
    for token in tokens:
        assert token not in caplog.text
        assert token[4:] not in caplog.text
        assert token.split(".")[-1] not in caplog.text


def test_mask_secrets_hides_jwt_and_password_values_BASE_03():
    jwt_text = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJlLXZhbHVl"
    unsigned = "eyJhbGciOiJub25lIn0.eyJzdWIiOiIxIn0."
    text = (
        f"Authorization: Bearer {jwt_text} other={unsigned} "
        '{"email": "a@example.com", "password": "Sec ret\\"9"} '
        "{'password': 'Pa55word'} password=FormPass1&next=1 PASSWORD: Upper99"
    )
    masked = mask_secrets(text)
    for secret in (jwt_text, "c2lnbmF0dXJlLXZhbHVl", unsigned, "Sec ret", "Pa55word", "FormPass1"):
        assert secret not in masked
    assert "Upper99" not in masked
    assert "a@example.com" in masked and "next=1" in masked
    assert "password_hash" in mask_secrets("column password_hash is bcrypt")


def test_email_auth_flow_logs_contain_no_password_or_jwt_BASE_03(client, caplog):
    password = "LogSecret77"
    with caplog.at_level(logging.DEBUG):
        created = signup(client, "logs@example.com", password).json()
        signup(client, "logs@example.com", password)  # 409
        signup(client, "bad@example.com", "short")  # 422
        logged_in = password_login(client, "logs@example.com", password).json()
        password_login(client, "logs@example.com", password + "x")  # 401
        client.get(ME, headers=bearer(logged_in["access_token"]))
        rotated = refresh(client, logged_in["refresh_token"]).json()
        client.delete(CURRENT_SESSION, headers=bearer(rotated["access_token"]))
        # 앱 로거로 요청 본문 모양의 원문이 들어와도 필터가 가린다.
        body = json.dumps({"password": password, "token": created["access_token"]})
        logging.getLogger("app").warning("request %s body=%s", "POST", body)
    assert caplog.records
    secrets_seen = [
        password,
        created["access_token"],
        logged_in["access_token"],
        rotated["access_token"],
        created["refresh_token"],
        logged_in["refresh_token"],
        rotated["refresh_token"],
    ]
    for secret in secrets_seen:
        assert secret not in caplog.text


@pytest.mark.parametrize(
    "logger_name",
    [
        "app.features.conversation.messaging.service",
        "app.features.creation.images.service",
        "uvicorn.protocols.http.h11_impl",
        "some_library.client",
    ],
)
def test_child_logger_records_are_masked_BASE_03(client, caplog, logger_name):
    """필터를 \"app\" 로거에만 붙이면 app.features.* 같은 하위 로거 기록은 가려지지 않는다.
    create_app 뒤에는 어느 로거에서 만든 기록이든 토큰·비밀번호가 가려진다."""
    fake_jwt = jwt.encode({"sub": "1", "sid": "2"}, "k" * 32, algorithm="HS256")
    fake_refresh = "ccr_" + "R7" * 21 + "x"
    password = "ChildSecret55"
    child = logging.getLogger(logger_name)
    with caplog.at_level(logging.INFO):
        child.warning("token=%s refresh=%s", fake_jwt, fake_refresh)
        child.warning('body {"email": "a@example.com", "password": "%s"}' % password)
        child.info("args %(auth)s", {"auth": f"Bearer {fake_jwt}"})
    assert len(caplog.records) == 3
    for secret in (fake_jwt, fake_jwt.split(".")[-1], fake_refresh, fake_refresh[4:], password):
        assert secret not in caplog.text
    assert "a@example.com" in caplog.text and "ccr_***" in caplog.text


def test_install_log_masking_wraps_factory_once_and_filter_still_masks_BASE_03(client):
    factory = logging.getLogRecordFactory()
    install_log_masking()
    assert logging.getLogRecordFactory() is factory  # 두 번 감싸지 않는다
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "t=%s", (REFRESH,), None)
    SecretMaskingFilter().filter(record)
    assert REFRESH not in record.getMessage()


def test_masking_placeholder_value_keeps_record_formattable_BASE_03(client, caplog):
    """`password=%s`처럼 가릴 자리에 자리표시자가 있어도 기록이 깨지지 않고 값이 가려진다
    (팩토리가 모든 로거에 적용되므로 외부 라이브러리 로그도 여기에 걸릴 수 있다)."""
    with caplog.at_level(logging.INFO):
        logging.getLogger("some_library.db").warning("login password=%s user=%s", "Pw12345", "bob")
    assert "Pw12345" not in caplog.text
    assert "password=*** user=bob" in caplog.text
