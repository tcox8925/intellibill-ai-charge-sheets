import json
import os
import base64
from typing import Any, Optional
from urllib import error, request


BASE_URL = os.environ.get("EXTERNAL_APIS_BASE_URL", "").rstrip("/")


class ExternalApiError(RuntimeError):
    pass


def _build_url(route: str) -> str:
    if not BASE_URL:
        raise ExternalApiError("EXTERNAL_APIS_BASE_URL is not configured")
    return f"{BASE_URL}/{route.lstrip('/')}"


def _decode_json(body: str) -> Optional[Any]:
    if not body:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


def _post_json(route: str, payload: dict,
               headers: Optional[dict[str, str]] = None):
    req_headers = {"content-type": "application/json"}
    if headers:
        req_headers.update(headers)
    req = request.Request(
        _build_url(route),
        data=json.dumps(payload).encode("utf-8"),
        headers=req_headers,
        method="POST",
    )
    try:
        with request.urlopen(req) as response:
            body = response.read().decode("utf-8")
            return response.status, _decode_json(body), body, response.headers
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, _decode_json(body), body, exc.headers


def _cookie_header_from_set_cookie(headers) -> str:
    set_cookies = headers.get_all("Set-Cookie") or []
    if not set_cookies:
        one_cookie = headers.get("Set-Cookie")
        if one_cookie:
            set_cookies = [one_cookie]

    cookies = []
    for item in set_cookies:
        cookie = item.split(";", 1)[0].strip()
        if cookie:
            cookies.append(cookie)
    return "; ".join(cookies)


def _token_cookie_from_headers(headers) -> Optional[str]:
    set_cookies = headers.get_all("Set-Cookie") or []
    if not set_cookies:
        one_cookie = headers.get("Set-Cookie")
        if one_cookie:
            set_cookies = [one_cookie]

    for item in set_cookies:
        if "TOKEN=" not in item:
            continue
        return item.split("TOKEN=", 1)[1].split(";", 1)[0]
    return None


def _decode_jwt_payload(token: str) -> dict:
    parts = token.split(".")
    if len(parts) != 3:
        raise ExternalApiError("Login failed: invalid token format")

    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload + padding)
        data = json.loads(decoded.decode("utf-8"))
    except (ValueError, json.JSONDecodeError) as exc:
        raise ExternalApiError("Login failed: unable to decode token") from exc
    if not isinstance(data, dict):
        raise ExternalApiError("Login failed: invalid token payload")
    return data


def login() -> dict:
    email = os.environ.get("RCM_SYSTEM_EMAIL", "")
    password = os.environ.get("RCM_SYSTEM_PASSWORD", "")
    if not email or not password:
        raise ExternalApiError(
            "RCM_SYSTEM_EMAIL and RCM_SYSTEM_PASSWORD must be configured"
        )

    status, _, body, headers = _post_json("auth.login?batch=1", {
        "0": {
            "email": email,
            "password": password,
        },
    })
    if status < 200 or status >= 300:
        raise ExternalApiError(f"Login failed: {status} {body}")

    cookie_header = _cookie_header_from_set_cookie(headers)
    if not cookie_header:
        raise ExternalApiError("Login failed: No cookies")

    token = _token_cookie_from_headers(headers)
    if not token:
        raise ExternalApiError("Login failed: No tokenCookie")

    user_id = _decode_jwt_payload(token).get("userId")
    if not user_id:
        raise ExternalApiError("Login failed: No userId")

    return {
        "cookie_header": cookie_header,
        "user_id": user_id,
    }


def queue_claim_creation(*, cookie_header: str, attachment_id: int,
                         run_async: bool = True):
    status, payload, body, _ = _post_json(
        "claims.createProfClaimFromDocument",
        {"attachmentId": attachment_id, "async": run_async},
        headers={"cookie": cookie_header},
    )
    if status < 200 or status >= 300:
        error_payload = payload if payload is not None else body
        raise ExternalApiError(
            f"Claim API failed: {status} {json.dumps(error_payload)}"
        )
    return payload if payload is not None else {"raw": body}