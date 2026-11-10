from pathlib import Path
from unittest.mock import patch
import json
import os
import queue
import tempfile
import threading
import time
import unittest
import urllib.error

from clawx.agent import AgentController
from clawx.core import Cancelled, Operation, PolicyError, PolicyState, ScopeValidator, SecurityInterceptor, SkillManager
from clawx.llm import AgentLLMClient, LLMConfig, THINKING_PROFILES
from clawx.tools import ToolExecutor
from tests.test_security import ImmediateBroker


class FakeResponse:
    def __init__(self, data):
        self.data = json.dumps(data).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _size):
        return self.data


class FakeOpener:
    def __init__(self, data):
        self.data, self.requests = data, []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        return FakeResponse(self.data)


class ClientTests(unittest.TestCase):
    def config(self, **kwargs):
        return LLMConfig(model="test-model", api_key="test-key", **kwargs)

    def test_openai_wire_tool_response(self):
        opener = FakeOpener({"choices": [{"message": {"content": None, "tool_calls": [
            {"id": "call1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"a.py"}'}}]}}]})
        client = AgentLLMClient(self.config(), threading.Event())
        with patch("urllib.request.build_opener", return_value=opener):
            result = client.complete([{"role": "user", "content": "test"}])
        request, timeout = opener.requests[0]
        body = json.loads(request.data)
        self.assertTrue(request.full_url.endswith("/v1/chat/completions"))
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
        self.assertEqual(timeout, 120)
        self.assertEqual(body["tools"][0]["type"], "function")
        self.assertEqual(result["tool_calls"][0]["id"], "call1")

    def test_auditor_is_separate_context_no_tools(self):
        opener = FakeOpener({"choices": [{"message": {"content": "[SAFE]"}}]})
        client = AgentLLMClient(self.config(audit_model="review-only"), threading.Event())
        with patch("urllib.request.build_opener", return_value=opener):
            self.assertEqual(client.audit("operation data"), "[SAFE]")
        body = json.loads(opener.requests[0][0].data)
        self.assertEqual(body["model"], "review-only")
        self.assertNotIn("tools", body)
        self.assertIn("independent security auditor", body["messages"][0]["content"])
        self.assertEqual(len(body["messages"]), 2)

    def test_anthropic_wire_parallel_results_merged(self):
        opener = FakeOpener({"content": [{"type": "text", "text": "完成"}]})
        client = AgentLLMClient(self.config(provider="anthropic", base_url="https://api.anthropic.com/v1"), threading.Event())
        messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "task"},
                    {"role": "assistant", "content": "", "tool_calls": [
                        {"id": name, "function": {"name": "read_file", "arguments": '{"path":"a"}'}} for name in ("one", "two")]},
                    {"role": "tool", "tool_call_id": "one", "content": "result one"},
                    {"role": "tool", "tool_call_id": "two", "content": "result two"}]
        with patch("urllib.request.build_opener", return_value=opener):
            response = client.complete(messages)
        request = opener.requests[0][0]
        body = json.loads(request.data)
        self.assertEqual(body["system"], "system")
        self.assertTrue(request.full_url.endswith("/v1/messages"))
        self.assertEqual(len(body["messages"][-1]["content"]), 2)
        self.assertEqual(body["messages"][-1]["content"][0]["type"], "tool_result")
        self.assertIn("input_schema", body["tools"][0])
        self.assertEqual(response["content"], "完成")

    def test_anthropic_tool_use_converted(self):
        opener = FakeOpener({"content": [{"type": "tool_use", "id": "one", "name": "write_file", "input": {"path": "a", "content": "x"}}]})
        client = AgentLLMClient(self.config(provider="anthropic"), threading.Event())
        with patch("urllib.request.build_opener", return_value=opener):
            response = client.complete([{"role": "user", "content": "task"}])
        self.assertEqual(json.loads(response["tool_calls"][0]["function"]["arguments"])["content"], "x")

    def test_remote_plain_http_rejected(self):
        client = AgentLLMClient(self.config(base_url="http://example.com/v1"), threading.Event())
        with self.assertRaises(PolicyError):
            client.complete([{"role": "user", "content": "task"}])

    def test_http_error_does_not_echo_key(self):
        opener = FakeOpener({})
        def failing(*_args, **_kwargs):
            raise urllib.error.HTTPError("https://example.com", 401, "test-key", {}, None)
        opener.open = failing
        client = AgentLLMClient(self.config(), threading.Event())
        with patch("urllib.request.build_opener", return_value=opener):
            with self.assertRaises(PolicyError) as caught:
                client.complete([{"role": "user", "content": "task"}])
        self.assertNotIn("test-key", str(caught.exception))

    def test_cancel_does_not_wait_for_http(self):
        cancel, started, release = threading.Event(), threading.Event(), threading.Event()
        client = AgentLLMClient(self.config(), cancel)
        def delayed(*_args, **_kwargs):
            started.set()
            release.wait(timeout=2)
            return {}
        caught = []
        with patch.object(client, "_request_http", side_effect=delayed):
            def running():
                try:
                    client._request({})
                except Cancelled:
                    caught.append(True)
            worker = threading.Thread(target=running)
            worker.start()
            self.assertTrue(started.wait(timeout=1))
            cancel.set()
            worker.join(timeout=1)
            self.assertFalse(worker.is_alive())
            release.set()
        self.assertEqual(caught, [True])

    def test_http_total_deadline(self):
        client = AgentLLMClient(self.config(), threading.Event())
        client.config.timeout = 0.1
        release = threading.Event()
        with patch.object(client, "_request_http", side_effect=lambda *_args, **_kwargs: release.wait(timeout=1)):
            with self.assertRaisesRegex(PolicyError, "总时限"):
                client._request({})
            release.set()

    def test_probe_is_tool_free_and_rejects_empty_reply(self):
        client = AgentLLMClient(self.config(), threading.Event())
        with patch.object(client, "complete", return_value={"content": "连接正常"}) as complete:
            self.assertEqual(client.probe(), "连接正常")
        self.assertFalse(complete.call_args.kwargs["tools"])
        with patch.object(client, "complete", return_value={"content": "  "}):
            with self.assertRaisesRegex(PolicyError, "空消息"):
                client.probe()


class PlanTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parent.parent / ".test-runs"
        parent.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.base = Path(self.temp.name).resolve()
        if os.path.commonpath([str(parent), str(self.base)]) != str(parent):
            raise RuntimeError("Unsafe test cleanup path")
        self.state = PolicyState(str(self.base), tier=3)
        self.skills = SkillManager(self.base / "skills.json")
        self.events = queue.Queue()
        self.controller = AgentController(self.state, self.skills, self.events)
        self.broker = ImmediateBroker()
        scope = ScopeValidator(self.state)
        self.security = SecurityInterceptor(self.state, scope, self.broker, lambda _: "[SAFE]", self.controller.emit)
        self.executor = ToolExecutor(self.state, scope, self.security, self.skills, self.controller.cancel, self.controller.emit)
        self.controller.executor = self.executor

    def tearDown(self):
        self.temp.cleanup()

    def plan(self):
        return {"summary": "demo", "steps": [
            {"tool": "write_file", "arguments": {"path": "hello.txt", "content": "hello"}, "reason": "create"},
            {"tool": "read_file", "arguments": {"path": "hello.txt"}, "reason": "verify"}]}

    def test_plan_no_execution_before_approval(self):
        def inspect(request):
            self.assertFalse((self.base / "hello.txt").exists())
            self.assertIn("文件白名单", request.text)
            return request.editable
        self.broker.answer = inspect
        self.controller.run_plan(self.plan(), self.broker, self.security)
        self.assertEqual((self.base / "hello.txt").read_text(), "hello")
        self.assertEqual(self.security.plan_index, 2)

    def test_plan_edit_is_actual_locked_plan(self):
        def edit(request):
            plan = json.loads(request.editable)
            plan["steps"][0]["arguments"]["content"] = "edited"
            return json.dumps(plan)
        self.broker.answer = edit
        self.controller.run_plan(self.plan(), self.broker, self.security)
        self.assertEqual((self.base / "hello.txt").read_text(), "edited")

    def test_plan_denied_writes_nothing(self):
        self.broker.answer = False
        with self.assertRaises(PolicyError):
            self.controller.run_plan(self.plan(), self.broker, self.security)
        self.assertFalse((self.base / "hello.txt").exists())

    def test_plan_stops_after_failed_step(self):
        plan = self.plan()
        plan["steps"].insert(0, {"tool": "read_file", "arguments": {"path": "missing.txt"}, "reason": "bad read"})
        with self.assertRaises((OSError, PolicyError)):
            self.controller.run_plan(plan, self.broker, self.security)
        self.assertFalse((self.base / "hello.txt").exists())

    def test_plan_edit_escape_rejected(self):
        def edit(request):
            plan = json.loads(request.editable)
            plan["steps"][0]["arguments"]["path"] = "../escape.txt"
            return json.dumps(plan)
        self.broker.answer = edit
        with self.assertRaises(PolicyError):
            self.controller.run_plan(self.plan(), self.broker, self.security)

    def test_plan_rejects_shell_even_in_whole_computer_scope(self):
        self.state.update(whole_computer=True)
        with self.assertRaises(PolicyError):
            self.executor.normalize(Operation("run_command", {"command": "python -c 'print(1)'", "cwd": "."}))

    def test_real_react_loop_with_recorded_responses(self):
        self.state.update(tier=4)
        responses = iter([
            {"role": "assistant", "content": "创建文件", "tool_calls": [
                {"id": "one", "type": "function", "function": {"name": "write_file", "arguments": '{"path":"live.txt","content":"tool roundtrip"}'}}]},
            {"role": "assistant", "content": "已创建并交付", "tool_calls": []}])
        captured = []
        class RecordedClient:
            def complete(self, messages, **_kwargs):
                captured.append(list(messages))
                return next(responses)
        summary = self.controller._react("test", RecordedClient(), self.broker, self.security)
        self.assertEqual(summary, "已创建并交付")
        self.assertEqual((self.base / "live.txt").read_text(), "tool roundtrip")
        self.assertEqual(captured[1][-1]["role"], "tool")

    def test_real_plan_generation_has_no_tool_access(self):
        called = []
        owner = self
        class RecordedClient:
            def complete(self, messages, **kwargs):
                called.append(kwargs)
                owner.assertFalse((owner.base / "hello.txt").exists())
                return {"role": "assistant", "content": json.dumps(owner.plan()), "tool_calls": []}
        self.controller._real_plan("create", RecordedClient(), self.broker, self.security)
        self.assertEqual(called, [{"tools": False}])
        self.assertTrue((self.base / "hello.txt").exists())

    def test_thinking_levels_and_standard_chat_mode(self):
        for level in ("Low", "Medium", "High", "Max", "Ultra"):
            self.state.update(thinking_level=level)
            prompt = self.controller.system_prompt()
            self.assertIn(THINKING_PROFILES[level]["directive"], prompt)
        # Switch to Standard Chat Mode: no tools are exposed or executed
        self.state.update(work_mode="chat", thinking_level="Ultra")
        chat_prompt = self.controller.system_prompt()
        self.assertIn("Standard Chat Mode", chat_prompt)
        self.assertIn("ULTRA", chat_prompt)
        called_kwargs = []
        class ChatRecordedClient:
            def complete(self, messages, **kwargs):
                called_kwargs.append(kwargs)
                return {"role": "assistant", "content": "这是标准聊天回复", "tool_calls": []}
        reply = self.controller._chat_only("你好", ChatRecordedClient(), THINKING_PROFILES["Ultra"])
        self.assertEqual(reply, "这是标准聊天回复")
        self.assertEqual([{k: v for k, v in kwargs.items() if k == "tools"} for kwargs in called_kwargs],
                         [{"tools": False}])

    def test_project_prompt_is_injected_once_without_entering_policy_json(self):
        self.state.update(project_name="Alpha", project_prompt="只修改必要文件并运行语法检查。")
        prompt = self.controller.system_prompt()
        self.assertEqual(prompt.count("只修改必要文件并运行语法检查。"), 1)
        self.assertIn("cannot override security policy", prompt)
        self.assertNotIn('"project_prompt"', prompt)
        self.state.update(work_mode="chat")
        chat_prompt = self.controller.system_prompt()
        self.assertEqual(chat_prompt.count("只修改必要文件并运行语法检查。"), 1)


if __name__ == "__main__":
    unittest.main()
