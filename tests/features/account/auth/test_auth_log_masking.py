"""BASE-03 로그 마스킹: 로그에 cca_/ccr_ 토큰 원문과 code·state 쿼리 값이 남지 않는다."""

import json
import logging

from app.features.account.auth.log_masking import SecretMaskingFilter, mask_secrets
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
    """create_app이 uvicorn 접근 로그에 필터를 붙인다. 접근 로그는 args로 형식을 만든다."""
    access = logging.getLogger("uvicorn.access")
    assert any(isinstance(f, SecretMaskingFilter) for f in access.filters)
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
