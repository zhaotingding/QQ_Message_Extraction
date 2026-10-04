"""大摘要上传回归：只连接本机临时 HTTP 服务，不调用真实模型。"""
from __future__ import annotations

import asyncio
import json
import sys
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

from aiohttp import web, ClientConnectorError, ServerDisconnectedError
from unittest.mock import patch, AsyncMock
from types import SimpleNamespace

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ / "src"))

from msgx.llm import LLM, RetryableRequestError, AmbiguousResponseError  # noqa: E402


REPLY = {"choices": [{"message": {"content": "完整摘要"}, "finish_reason": "stop"}]}


def payload(large: bool = True) -> dict:
    text = '中文消息、关键句与主题分析。ASCII 123 "quoted" \\ newline\n🙂'
    return {"model": "local-test-only", "messages": [
        {"role": "system", "content": "按主题概括全部消息"},
        {"role": "user", "content": text * (3500 if large else 1)},
    ], "max_tokens": 10240, "stream": False, "reasoning_effort": "none"}


@asynccontextmanager
async def local_llm(handler):
    app = web.Application(client_max_size=2 * 1024 * 1024)
    app.router.add_post("/{path:.*}", handler)
    runner = web.AppRunner(app, shutdown_timeout=1)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    llm = LLM({"llm": {"base_url": f"http://127.0.0.1:{port}",
                       "post_retries": 2, "timeout_seconds": 5}}, None)
    llm.configure("local-test-key")
    try:
        yield llm
    finally:
        await llm.close()
        await runner.cleanup()


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def collect(self, statuses, large=True):
        received = []

        async def handler(request):
            body = await request.read()
            received.append((request.path, dict(request.headers), body))
            status = statuses[min(len(received) - 1, len(statuses) - 1)]
            return web.json_response(REPLY, status=status)

        data = payload(large)
        async with local_llm(handler) as llm:
            result = await llm._post(data)
        self.assertEqual(result, REPLY)
        expected = json.dumps(data).encode("utf-8")
        for _, headers, body in received:
            self.assertEqual(body, expected, "包括中文、换行和转义字符的完整请求必须原样送达")
            self.assertEqual(json.loads(body), data)
            self.assertEqual(int(headers["Content-Length"]), len(expected))
            self.assertNotIn("Transfer-Encoding", headers, "分段写出仍须是有完整长度的单次 JSON 请求")
            self.assertEqual(headers["Content-Type"], "application/json")
        return received

    async def test_large_single_request_preserves_full_body(self):
        received = await self.collect([200])
        self.assertEqual(len(received), 1, "HTTP 上传小段不能变成多次模型请求")
        self.assertGreater(len(received[0][2]), 450_000)

    async def test_small_single_request(self):
        received = await self.collect([200], large=False)
        self.assertEqual(len(received), 1)
        self.assertLess(len(received[0][2]), 64 * 1024)

    async def test_chat_sse_over_real_http_handles_fragmented_utf8(self):
        received = []
        async def handler(request):
            received.append(await request.json())
            r = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await r.prepare(request)
            event = {"choices": [{"index": 0, "delta": {"content": "中文完整摘要🙂"}, "finish_reason": "stop"}]}
            body = (": heartbeat\r\n\r\ndata: " + json.dumps(event, ensure_ascii=False) +
                    '\r\n\r\ndata: {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2}}\r\n\r\n'
                    'data: [DONE]\r\n\r\n').encode("utf-8")
            for offset in range(0, len(body), 7):
                await r.write(body[offset:offset + 7])
                await asyncio.sleep(0)
            return r
        async with local_llm(handler) as llm:
            result = await llm._post({**payload(False), "stream": True})
        self.assertEqual(result["choices"][0]["message"]["content"], "中文完整摘要🙂")
        self.assertEqual(result["usage"]["prompt_tokens"], 10)
        self.assertEqual(len(received), 1)

    async def test_404_fallback_replays_complete_upload(self):
        received = await self.collect([404, 200])
        self.assertEqual([row[0] for row in received],
                         ["/chat/completions", "/v1/chat/completions"])

    async def test_transient_retry_replays_complete_upload(self):
        received = await self.collect([503, 200])
        self.assertEqual([row[0] for row in received],
                         ["/chat/completions", "/chat/completions"])

    async def test_rejected_transient_request_has_finite_retries(self):
        for status in (429, 503):
            calls = []

            async def handler(request):
                calls.append(await request.read())
                return web.json_response({"error": {"type": "tokens"}}, status=status)

            async with local_llm(handler) as llm:
                with self.assertRaises(RetryableRequestError):
                    await llm._post(payload(False))
            self.assertEqual(len(calls), 2)

    async def test_success_status_with_partial_json_is_never_reposted(self):
        calls = []

        async def handler(request):
            calls.append(await request.read())
            return web.Response(text='{"choices": [')

        async with local_llm(handler) as llm:
            with self.assertRaises(AmbiguousResponseError):
                await llm._post(payload(False))
        self.assertEqual(len(calls), 1)

    async def test_connection_failure_and_ambiguous_failure_are_distinct(self):
        for error, expected, attempts in (
            (ClientConnectorError(SimpleNamespace(host="local.invalid", port=80, ssl=False),
                                  OSError("connection refused")), RetryableRequestError, 2),
            (ServerDisconnectedError(), AmbiguousResponseError, 1),
            (asyncio.TimeoutError(), AmbiguousResponseError, 1),
        ):
            calls = []

            class Session:
                closed = False

                @asynccontextmanager
                async def post(self, *args, **kwargs):
                    calls.append(True)
                    raise error
                    yield  # pragma: no cover

            llm = LLM({"llm": {"base_url": "http://local.invalid", "post_retries": 2}}, None)
            llm._session = Session()
            with patch("msgx.llm.asyncio.sleep", new=AsyncMock()):
                with self.assertRaises(expected):
                    await llm._post(payload(False))
            self.assertEqual(len(calls), attempts)

    async def test_large_upload_yields_control_between_small_writes(self):
        """用受控 HTTP 客户端检查实际写出节奏，不依赖机器速度或时延阈值。"""
        test = self
        seen = []

        class Response:
            status = 200

            async def text(self):
                return json.dumps(REPLY)

        class Session:
            closed = False

            @asynccontextmanager
            async def post(self, url, **kwargs):
                body = kwargs.get("data")
                test.assertTrue(hasattr(body, "__aiter__"), "大请求需要受控分段上传，不能一次性写出")
                pieces = []
                iterator = body.__aiter__()
                while True:
                    scheduled = []
                    asyncio.get_running_loop().call_soon(scheduled.append, True)
                    try:
                        piece = await anext(iterator)
                    except StopAsyncIteration:
                        break
                    test.assertTrue(scheduled, "每一段之间须交还事件循环，防止一次性写满缓冲区")
                    test.assertGreater(len(piece), 0)
                    test.assertLessEqual(len(piece), 8192)
                    pieces.append(piece)
                seen.append((kwargs["headers"], b"".join(pieces)))
                yield Response()

        data = payload()
        llm = LLM({"llm": {"base_url": "http://local.invalid"}}, None)
        llm._session = Session()
        self.assertEqual(await llm._post_once(data), REPLY)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][1], json.dumps(data).encode("utf-8"))
        self.assertEqual(int(seen[0][0]["Content-Length"]), len(seen[0][1]))

    async def test_cancel_upload_releases_connection_without_retry(self):
        first_bytes = asyncio.Event()
        first_done = asyncio.Event()
        paths = []

        async def handler(request):
            paths.append(request.path)
            if len(paths) == 1:
                try:
                    await request.content.readany()
                    first_bytes.set()
                    await request.read()
                except (ConnectionError, asyncio.CancelledError):
                    pass
                finally:
                    first_done.set()
            else:
                await request.read()
            return web.json_response(REPLY)

        async with local_llm(handler) as llm:
            request = asyncio.create_task(llm._post(payload()))
            await asyncio.wait_for(first_bytes.wait(), timeout=3)
            request.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await request
            await asyncio.wait_for(first_done.wait(), timeout=3)
            self.assertEqual(paths, ["/chat/completions"], "取消不能变成自动重试")
            self.assertFalse(llm._session.closed, "单次取消不能关闭其他请求共享的会话")
            self.assertEqual(await llm._post(payload(False)), REPLY)
            self.assertEqual(len(paths), 2, "取消后下一次请求仍可正常使用连接池")
            self.assertFalse(llm._session.connector._acquired, "取消和完成后不得泄漏占用的连接")


if __name__ == "__main__":
    unittest.main(verbosity=2)
