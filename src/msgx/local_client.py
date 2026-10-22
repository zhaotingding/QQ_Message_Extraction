"""GUI 的本地服务 JSON 请求；绕过系统代理，不重试或决定业务反馈。"""
from __future__ import annotations

import json
import urllib.request
from urllib.parse import urlencode


def request_json(port: int, path: str, *, method: str = "GET", payload=None,
                 query: dict | None = None, timeout: float, token: str = ""):
    """同步工作线程入口；HTTPError 保留正文，调用者决定如何展示未知结果。"""
    url = f"http://127.0.0.1:{port}{path}"
    if query:
        url += "?" + urlencode(query)
    headers = {}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif method == "POST":
        data = b""
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))
