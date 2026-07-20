import requests
import json
import time
import os
from dataclasses import dataclass, field
from typing import Optional

CONFIG_DIR = os.path.expanduser("~/.config/kook-linux")
SESSION_FILE = os.path.join(CONFIG_DIR, "session.json")

V2_BASE = "https://www.kookapp.cn/api/v2"
V3_BASE = "https://www.kookapp.cn/api/v3"


@dataclass
class KookSession:
    user_id: str
    username: str
    cookies: dict = field(default_factory=dict)
    token: str = ""
    login_time: float = 0.0
    session_file: str = SESSION_FILE

    def save(self, path: Optional[str] = None):
        p = path or self.session_file
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            json.dump({
                "user_id": self.user_id,
                "username": self.username,
                "cookies": self.cookies,
                "token": self.token,
                "login_time": self.login_time,
            }, f)

    @staticmethod
    def load(path: Optional[str] = None) -> Optional["KookSession"]:
        p = path or SESSION_FILE
        try:
            with open(p) as f:
                d = json.load(f)
            return KookSession(
                user_id=d["user_id"],
                username=d["username"],
                cookies=d.get("cookies", {}),
                token=d.get("token", ""),
                login_time=d.get("login_time", 0),
            )
        except (FileNotFoundError, KeyError, json.JSONDecodeError):
            return None


def create_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) KookLinux/1.0",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    return s


LOGIN_ENDPOINT = f"{V2_BASE}/auth/login"


def login(phone: str, password: str, mobile_prefix: str = "86",
          endpoint: Optional[str] = None) -> KookSession:
    session = create_session()

    session.headers.update({
        "Origin": "https://www.kookapp.cn",
        "Referer": "https://www.kookapp.cn/app/passwordlogin",
    })

    url = endpoint or LOGIN_ENDPOINT
    payload = {
        "mobile": phone,
        "password": password,
        "mobile_prefix": mobile_prefix,
        "remember": True,
    }

    resp = session.post(url, json=payload, timeout=15)
    if resp.status_code != 200:
        raise RuntimeError(f"Login failed (HTTP {resp.status_code}): {resp.text[:200]}")

    body = resp.json()
    if "token" in body:
        token = body["token"]
    elif body.get("code") == 0:
        token = body.get("data", {}).get("token", "")
    else:
        raise RuntimeError(f"Login failed: {body.get('message', body)}")

    if not token:
        raise RuntimeError(f"No token in response: {body}")

    session.headers["Authorization"] = token

    user_data = body.get("user")
    if not user_data:
        user_data = _get_user_info(session)
    if not user_data:
        raise RuntimeError("Login succeeded but failed to get user info")

    cookies = session.cookies.get_dict()

    ks = KookSession(
        user_id=user_data["id"],
        username=user_data["username"],
        token=token,
        cookies=cookies,
        login_time=time.time(),
    )
    ks.save()
    return ks


def _get_user_info(session: requests.Session) -> Optional[dict]:
    try:
        resp = session.get(f"{V3_BASE}/user/me", timeout=15)
        if resp.status_code == 200:
            body = resp.json()
            if body.get("code") == 0:
                return body["data"]
    except Exception:
        pass
    return None


def login_with_token(token: str, token_type: str = "raw") -> KookSession:
    session = create_session()
    auth_map = {"bot": f"Bot {token}", "bearer": f"Bearer {token}", "raw": token}
    session.headers["Authorization"] = auth_map.get(token_type, token)

    user_info = _get_user_info(session)
    if not user_info:
        raise RuntimeError("Token validation failed")
    ks = KookSession(
        user_id=user_info["id"],
        username=user_info["username"],
        token=token,
        login_time=time.time(),
    )
    ks.save()
    return ks
