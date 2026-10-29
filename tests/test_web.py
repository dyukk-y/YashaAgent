import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest
from fastapi import HTTPException

from app.web import verify_init_data


def signed_init_data(bot_token: str, user_id: int) -> str:
    values = {
        "auth_date": str(int(time.time())),
        "query_id": "AAExample",
        "user": json.dumps({"id": user_id, "first_name": "Test"}, separators=(",", ":")),
    }
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


def test_verify_telegram_webapp_init_data():
    data = signed_init_data("test-token", 12345)
    user = verify_init_data(data, "test-token")
    assert user["id"] == 12345
    assert user["first_name"] == "Test"


def test_reject_tampered_telegram_webapp_init_data():
    data = signed_init_data("test-token", 12345).replace("12345", "54321")
    with pytest.raises(HTTPException) as exc:
        verify_init_data(data, "test-token")
    assert exc.value.status_code == 401
