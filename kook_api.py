import requests
from http.cookiejar import Cookie
from typing import Optional, Any
from kook_auth import KookSession, V3_BASE


class KookAPI:
    def __init__(self, session: KookSession):
        self.session = session
        self.http = requests.Session()
        self.http.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh",
            "Origin": "https://www.kookapp.cn",
            "Referer": "https://www.kookapp.cn/app/passwordlogin",
        })

        if session.token:
            self.http.headers["Authorization"] = session.token
        if session.cookies:
            for name, value in session.cookies.items():
                c = Cookie(
                    version=0, name=name, value=value,
                    port=None, port_specified=False,
                    domain="kookapp.cn", domain_specified=True,
                    domain_initial_dot=False,
                    path="/", path_specified=True,
                    secure=True, expires=None, discard=True,
                    comment=None, comment_url=None,
                    rest={},
                    rfc2109=False,
                )
                self.http.cookies.set_cookie(c)

    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{V3_BASE}{path}"
        kwargs.setdefault("timeout", 15)
        resp = self.http.request(method, url, **kwargs)
        body = resp.json()
        if body.get("code") != 0:
            raise RuntimeError(f"API error [{path}]: {body.get('message', body)}")
        return body["data"]

    def get_guilds(self) -> list[dict]:
        data = self._request("GET", "/guild/list")
        return data.get("items", data if isinstance(data, list) else [])

    def get_channels(self, guild_id: str, channel_type: int = 2) -> list[dict]:
        data = self._request("GET", "/channel/list", params={
            "guild_id": guild_id,
            "type": channel_type,
        })
        return data.get("items", data if isinstance(data, list) else [])

    def get_channel_users(self, channel_id: str) -> list[dict]:
        data = self._request("GET", "/channel/user-list", params={
            "channel_id": channel_id,
        })
        return data if isinstance(data, list) else []

    def join_voice(self, channel_id: str, password: Optional[str] = None) -> dict:
        payload = {"channel_id": channel_id}
        if password:
            payload["password"] = password
        return self._request("POST", "/voice/join", json=payload)

    def leave_voice(self, channel_id: str):
        self._request("POST", "/voice/leave", json={"channel_id": channel_id})

    def keep_alive(self, channel_id: str):
        self._request("POST", "/voice/keep-alive", json={"channel_id": channel_id})

    def list_voice_channels(self) -> list[dict]:
        data = self._request("GET", "/voice/list")
        return data.get("items", data if isinstance(data, list) else [])

    def move_user(self, target_channel_id: str, user_ids: list[str]):
        self._request("POST", "/channel/move-user", json={
            "target_id": target_channel_id,
            "user_ids": user_ids,
        })

    def kick_user(self, channel_id: str, user_id: str):
        self._request("POST", "/channel/kickout", json={
            "channel_id": channel_id,
            "user_id": user_id,
        })

    # --- Message API ---

    def get_messages(self, channel_id: str, page_size: int = 50, oldest_first: bool = True) -> list[dict]:
        data = self._request("GET", "/message/list", params={
            "target_id": channel_id,
            "page_size": page_size,
            "oldest_first": str(oldest_first).lower(),
        })
        # data may be a list or have 'items' key
        return data if isinstance(data, list) else data.get("items", [])

    def send_message(self, channel_id: str, content: str, quote: Optional[str] = None) -> dict:
        payload = {"type": 1, "target_id": channel_id, "content": content}
        if quote:
            payload["quote"] = quote
        return self._request("POST", "/message/create", json=payload)

    def get_gateway_index(self) -> str:
        return self._request("GET", "/gateway/index")["url"]
