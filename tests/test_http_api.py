"""End-to-end HTTP checks: a user-provided OpenAI-compatible / Anthropic API
must drive real conversations through the same urllib path the app uses."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import queue
import tempfile
import threading
import unittest
from unittest.mock import patch

from clawx.agent import AgentController
from clawx.core import Cancelled, PolicyError, PolicyState, SkillManager
from clawx.llm import AgentLLMClient, LLMConfig


class FakeAPIHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):  # silence
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.requests.append((self.path, self.headers, body))
        if self.server.ignore_stream:
            body = {**body, "stream": False}
        if body.get("stream") and self.server.stream_script is not None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for index, item in enumerate(self.server.stream_script):
                raw = item if isinstance(item, str) else json.dumps(item)
                self.wfile.write(("data: " + raw + "\n\n").encode())
                self.wfile.flush()
                if index == self.server.pause_after:
                    self.server.release.wait(timeout=4)
            return
        if self.path.endswith("/v1/chat/completions"):
            if body.get("stream"):
                content = body["messages"][-1].get("content") or ""
                text = "echo:" + str(content)[:60]
                deltas = [{"choices": [{"delta": {"content": text[i:i+2]}}]}
                          for i in range(0, len(text), 2)]
                deltas.append({"choices": [{"delta": {}}]})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for item in deltas:
                    self.wfile.write(b"data: " + json.dumps(item).encode() + b"\n\n")
                self.wfile.write(b"data: [DONE]\n\n")
                return
            content = body["messages"][-1].get("content") or ""
            reply = {"choices": [{"message": {"content": "echo:" + str(content)[:60], "tool_calls": []}}]}
        elif self.path.endswith("/v1/messages"):
            text = ""
            for message in reversed(body.get("messages", [])):
                text = next((b.get("text") for b in message.get("content", [])
                             if b.get("type") == "text"), "")
                if text:
                    break
            text = "echo:" + str(text)[:60]
            if body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                events = [{"type": "content_block_start", "index": 0,
                           "content_block": {"type": "text", "text": ""}}]
                events.extend({"type": "content_block_delta", "index": 0,
                               "delta": {"type": "text_delta", "text": text[i:i+2]}}
                              for i in range(0, len(text), 2))
                events.append({"type": "message_stop"})
                for event in events:
                    self.wfile.write(b"data: " + json.dumps(event).encode() + b"\n\n")
                    self.wfile.flush()
                return
            reply = {"content": [{"type": "text", "text": text}]}
        else:
            self.send_response(404)
            self.end_headers()
            return
        raw = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class HTTPAPITests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeAPIHandler)
        self.server.requests = []
        self.server.stream_script = None
        self.server.ignore_stream = False
        self.server.pause_after = -1
        self.server.release = threading.Event()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}/v1"
        parent = Path(__file__).resolve().parent.parent / ".test-runs"
        parent.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)

    def tearDown(self):
        self.server.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def test_openai_compatible_chat_completion(self):
        config = LLMConfig(provider="openai", base_url=self.base,
                           model="user-model", api_key="user-key", timeout=30)
        client = AgentLLMClient(config, threading.Event())
        reply = client.complete([{"role": "user", "content": "你好"}], tools=False)
        self.assertEqual(reply["content"], "echo:你好")
        self.assertEqual(reply["tool_calls"], [])
        path, headers, body = self.server.requests[0]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(headers.get("Authorization"), "Bearer user-key")
        self.assertEqual(body["model"], "user-model")

    def test_openai_streaming_completion(self):
        config = LLMConfig(provider="openai", base_url=self.base,
                           model="user-model", api_key="user-key", timeout=30)
        client = AgentLLMClient(config, threading.Event())
        tokens = []
        reply = client.complete([{"role": "user", "content": "你好"}], tools=False,
                                stream=True, on_token=tokens.append)
        self.assertEqual("".join(tokens), "echo:你好")
        self.assertEqual(reply["content"], "echo:你好")
        self.assertEqual(reply["tool_calls"], [])
        self.assertTrue(self.server.requests[0][2]["stream"])

    def test_anthropic_messages_completion(self):
        config = LLMConfig(provider="anthropic", base_url=self.base,
                           model="claude-model", api_key="user-key", timeout=30)
        client = AgentLLMClient(config, threading.Event())
        reply = client.complete([{"role": "user", "content": "hello"}], tools=False)
        self.assertEqual(reply["content"], "echo:hello")
        path, headers, body = self.server.requests[0]
        self.assertEqual(path, "/v1/messages")
        self.assertEqual(headers.get("x-api-key"), "user-key")
        self.assertEqual(body["model"], "claude-model")

    def assert_live_stream(self, provider):
        if provider == "openai":
            script = [{"choices": [{"delta": {"content": "首段"}}]},
                      {"choices": [{"delta": {"content": "末段"}}]}, "[DONE]"]
            pause_after = 0
        else:
            script = [{"type": "content_block_start", "index": 0,
                       "content_block": {"type": "text", "text": ""}},
                      {"type": "content_block_delta", "index": 0,
                       "delta": {"type": "text_delta", "text": "首段"}},
                      {"type": "content_block_delta", "index": 0,
                       "delta": {"type": "text_delta", "text": "末段"}},
                      {"type": "message_stop"}]
            pause_after = 1
        self.server.stream_script, self.server.pause_after = script, pause_after
        first, tokens, answers = threading.Event(), [], queue.Queue()
        client = AgentLLMClient(LLMConfig(provider=provider, base_url=self.base,
                                         model="test", timeout=10), threading.Event())

        def token(piece):
            tokens.append(piece)
            first.set()

        def complete():
            try:
                answers.put(client.complete([{"role": "user", "content": "hello"}],
                                            stream=True, on_token=token))
            except Exception as exc:
                answers.put(exc)
        worker = threading.Thread(target=complete)
        worker.start()
        try:
            self.assertTrue(first.wait(timeout=2), "first token must arrive before response ends")
            self.assertEqual(tokens, ["首段"])
            self.assertTrue(answers.empty())
        finally:
            self.server.release.set()
            worker.join(timeout=3)
        reply = answers.get(timeout=1)
        self.assertIsInstance(reply, dict)
        self.assertEqual(reply["content"], "首段末段")
        self.assertEqual("".join(tokens), reply["content"])

    def test_openai_first_token_arrives_before_completion(self):
        self.assert_live_stream("openai")

    def test_anthropic_first_token_arrives_before_completion(self):
        self.assert_live_stream("anthropic")

    def test_gateway_returning_json_despite_stream_remains_usable(self):
        self.server.ignore_stream = True
        for provider in ("openai", "anthropic"):
            with self.subTest(provider=provider):
                client = AgentLLMClient(LLMConfig(provider=provider, base_url=self.base,
                                                 model="test"), threading.Event())
                reply = client.complete([{"role": "user", "content": "hello"}],
                                        tools=False, stream=True, on_token=lambda _text: None)
                self.assertEqual(reply["content"], "echo:hello")
                self.assertTrue(self.server.requests[-1][2]["stream"])

    def test_streamed_openai_tool_arguments_are_assembled(self):
        self.server.stream_script = [
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call1",
             "function": {"name": "list_files", "arguments": '{"path":'}}]}}]},
            {"choices": [], "usage": {"total_tokens": 4}},
            {"choices": [{"delta": {"tool_calls": [{"index": 0,
             "function": {"arguments": '"."}'}}]}}]}, "[DONE]"]
        client = AgentLLMClient(LLMConfig(base_url=self.base, model="test"), threading.Event())
        tokens = []
        reply = client.complete([], stream=True, on_token=tokens.append)
        self.assertEqual(tokens, [])
        call = reply["tool_calls"][0]
        self.assertEqual(call["id"], "call1")
        self.assertEqual(call["function"], {"name": "list_files", "arguments": '{"path":"."}'})

    def test_claude_stream_preserves_private_signed_blocks_and_tools(self):
        self.server.stream_script = [
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "thinking_delta", "thinking": "private"}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "signature_delta", "signature": "signed"}},
            {"type": "content_block_start", "index": 1,
             "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 1,
             "delta": {"type": "text_delta", "text": "正在查看"}},
            {"type": "content_block_start", "index": 2,
             "content_block": {"type": "tool_use", "id": "tool1", "name": "list_files", "input": {}}},
            {"type": "content_block_delta", "index": 2,
             "delta": {"type": "input_json_delta", "partial_json": '{"path":'}},
            {"type": "content_block_delta", "index": 2,
             "delta": {"type": "input_json_delta", "partial_json": '"."}'}},
            {"type": "message_stop"}]
        client = AgentLLMClient(LLMConfig(provider="anthropic", base_url=self.base,
                                         model="test"), threading.Event())
        tokens = []
        reply = client.complete([], stream=True, on_token=tokens.append)
        self.assertEqual(tokens, ["正在查看"])
        self.assertEqual(reply["provider_content"][0],
                         {"type": "thinking", "thinking": "private", "signature": "signed"})
        self.assertEqual(json.loads(reply["tool_calls"][0]["function"]["arguments"]), {"path": "."})
        _, messages = client._anthropic_messages([reply])
        self.assertEqual(messages[0]["content"], reply["provider_content"])

    def test_truncated_stream_and_provider_errors_fail_closed(self):
        scripts = [
            [{"choices": [{"delta": {"content": "partial"}}]}],
            [{"type": "error", "error": {"message": "SECRET_ERROR_BODY"}}],
            ["invalid-json"]]
        client = AgentLLMClient(LLMConfig(base_url=self.base, model="test"), threading.Event())
        for script in scripts:
            with self.subTest(script=script):
                self.server.stream_script = script
                with self.assertRaises(PolicyError) as error:
                    client.complete([], stream=True, on_token=lambda _text: None)
                self.assertNotIn("SECRET_ERROR_BODY", str(error.exception))

    def test_cancelled_http_helper_cannot_publish_late_tokens(self):
        cancel, entered, release = threading.Event(), threading.Event(), threading.Event()
        helper_done = threading.Event()
        client = AgentLLMClient(LLMConfig(model="test"), cancel)
        tokens, answers = [], queue.Queue()

        def delayed(_payload, callback, **_kwargs):
            callback("first")
            entered.set()
            release.wait(timeout=3)
            callback("late")
            helper_done.set()
            return {}

        def request():
            try:
                answers.put(client._request_stream({}, tokens.append))
            except Exception as exc:
                answers.put(exc)

        with patch.object(client, "_request_http_stream", delayed):
            worker = threading.Thread(target=request)
            worker.start()
            try:
                self.assertTrue(entered.wait(timeout=2))
                cancel.set()
                worker.join(timeout=2)
                self.assertIsInstance(answers.get(timeout=1), Cancelled)
                before = tokens[:]
                cancel.clear()  # A new task may reuse the cancellation event.
            finally:
                release.set()
                worker.join(timeout=2)
                self.assertTrue(helper_done.wait(timeout=2))
        self.assertEqual(tokens, before)

    def test_agent_chat_uses_user_api_end_to_end(self):
        base = Path(self.temp.name)
        state = PolicyState(str(base), tier=4)
        state.update(work_mode="chat", thinking_level="Medium")
        events = queue.Queue()
        controller = AgentController(state, SkillManager(base / "skills.json"), events)
        config = LLMConfig(provider="openai", base_url=self.base,
                           model="user-model", api_key="user-key", timeout=30,
                           work_mode="chat", chat_style="deep")
        controller.start("请回答", config)
        controller.thread.join(timeout=10)
        self.assertFalse(controller.running)
        items = []
        while not events.empty():
            items.append(events.get_nowait())
        self.assertEqual([kind for kind, _ in items if kind == "done"], ["done"])
        # 聊天回复以流式 token 输出，聚合后与完整回复一致。
        tokens = [payload for kind, payload in items if kind == "token"]
        self.assertGreaterEqual(len(tokens), 1)
        self.assertEqual("".join(t["text"] for t in tokens), "echo:请回答")
        self.assertIn("stream_end", [kind for kind, _ in items])
        reports = [value for kind, value in items if kind == "deliverable"]
        self.assertEqual(len(reports), 1)
        self.assertIn("本次对话没有读写任何文件", reports[0])


if __name__ == "__main__":
    unittest.main()
