"""Behavioral checks for mode isolation and provider reasoning requests."""
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
import json
import queue
import tempfile
import threading
import unittest
from types import SimpleNamespace

from clawx.agent import AgentController
from clawx.core import Cancelled, Operation, PolicyError, PolicyState, SkillManager
from clawx.llm import AgentLLMClient, LLMConfig, THINKING_PROFILES
from tests.test_agent_llm import FakeOpener


class ReasoningRequestTests(unittest.TestCase):
    def request(self, config, response=None, audit=False):
        opener = FakeOpener(response or {"choices": [{"message": {"content": "ok"}}]})
        client = AgentLLMClient(config, threading.Event())
        with patch("urllib.request.build_opener", return_value=opener):
            client.complete([{"role": "user", "content": "test"}], tools=False, audit=audit)
        return json.loads(opener.requests[0][0].data)

    def test_openai_all_five_levels_and_ceiling(self):
        for level, effort in zip(THINKING_PROFILES, ("low", "medium", "high", "xhigh", "max")):
            with self.subTest(level=level):
                config = LLMConfig(model="test", reasoning_api="openai",
                                   thinking_level=level, effort_ceiling="max")
                body = self.request(config)
                self.assertEqual(body["reasoning_effort"], effort)
                self.assertGreater(body["max_completion_tokens"], THINKING_PROFILES[level]["budget_tokens"])
        config.effort_ceiling = "high"
        self.assertEqual(self.request(config)["reasoning_effort"], "high")

    def test_chat_style_controls_effective_native_effort_without_losing_selection(self):
        config = LLMConfig(model="test", reasoning_api="openai", work_mode="chat",
                           thinking_level="Ultra", effort_ceiling="max")
        # 即时回答只使用 标准/中/高（Low/Medium/High）：Ultra 被钳制为 High。
        self.assertEqual(self.request(config)["reasoning_effort"], "high")
        self.assertEqual(self.request(replace(config, chat_style="deep"))["reasoning_effort"], "max")
        self.assertEqual(config.thinking_level, "Ultra")

    def test_portable_and_auditor_do_not_receive_native_parameters(self):
        config = LLMConfig(model="test", thinking_level="Ultra")
        for body in (self.request(config), self.request(replace(config, reasoning_api="openai"), audit=True)):
            self.assertNotIn("reasoning_effort", body)
            self.assertNotIn("max_completion_tokens", body)
            self.assertNotIn("tools", body)

    def test_anthropic_adaptive_and_manual_are_explicit(self):
        config = LLMConfig(provider="anthropic", model="test", thinking_level="Ultra",
                           reasoning_api="anthropic", effort_ceiling="max")
        body = self.request(config, {"content": [{"type": "text", "text": "ok"}]})
        self.assertEqual(body["thinking"], {"type": "adaptive"})
        self.assertEqual(body["output_config"]["effort"], "max")
        for level, profile in THINKING_PROFILES.items():
            body = self.request(replace(config, reasoning_api="anthropic_manual", thinking_level=level),
                                {"content": [{"type": "text", "text": "ok"}]})
            self.assertEqual(body["thinking"]["budget_tokens"], profile["budget_tokens"])
            self.assertGreater(body["max_tokens"], body["thinking"]["budget_tokens"])

    def test_claude_signed_blocks_preserved_only_in_protocol_context(self):
        blocks = [{"type": "thinking", "thinking": "provider summary", "signature": "signed"},
                  {"type": "tool_use", "id": "call", "name": "read_file", "input": {"path": "a"}}]
        config = LLMConfig(provider="anthropic", model="test", reasoning_api="anthropic")
        client = AgentLLMClient(config, threading.Event())
        opener = FakeOpener({"content": blocks})
        messages = [{"role": "user", "content": "test"}]
        with patch("urllib.request.build_opener", return_value=opener):
            response = client.complete(messages)
            messages.extend([response, {"role": "tool", "tool_call_id": "call", "content": "result"}])
            client.complete(messages)
        body = json.loads(opener.requests[-1][0].data)
        self.assertEqual(body["messages"][1]["content"], blocks)
        self.assertNotIn("provider summary", response["content"])

    def test_invalid_profiles_fail_before_network(self):
        for overrides in ({"thinking_level": "invalid"}, {"work_mode": "invalid"}, {"chat_style": "cowork"},
                          {"reasoning_api": "anthropic"}, {"effort_ceiling": "ultra"}):
            with self.subTest(overrides=overrides), self.assertRaises(PolicyError):
                self.request(LLMConfig(model="test", **overrides))


class ModeIsolationTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parent.parent / ".test-runs"
        parent.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.base = Path(self.temp.name)
        self.state = PolicyState(str(self.base), tier=4)
        self.events = queue.Queue()
        self.agent = AgentController(self.state, SkillManager(self.base / "skills.json"), self.events)

    def tearDown(self):
        self.agent.stop()
        if self.agent.thread:
            self.agent.thread.join(timeout=5)
        self.temp.cleanup()

    def run_task(self, config):
        self.agent.start("test", config)
        self.agent.thread.join(timeout=5)
        self.assertFalse(self.agent.running)
        items = []
        while not self.events.empty():
            items.append(self.events.get_nowait())
        return items

    def test_chat_rejects_unexpected_tool_calls_without_executing(self):
        self.state.update(work_mode="chat")
        response = {"content": "", "tool_calls": [{"function": {"name": "write_file",
                    "arguments": '{"path":"leak.txt","content":"bad"}'}}]}
        with patch.object(AgentLLMClient, "complete", return_value=response) as call, \
                patch("clawx.agent.ToolExecutor.execute") as execute:
            items = self.run_task(LLMConfig(work_mode="chat", model="test"))
        self.assertEqual(call.call_args.kwargs["tools"], False)
        execute.assert_not_called()
        self.assertFalse((self.base / "leak.txt").exists())
        self.assertFalse(any(kind == "approval" for kind, _ in items))
        self.assertIn(("done", "未完成"), items)

    def test_mode_contexts_are_separate_and_chat_reply_emitted_once(self):
        self.agent.histories["work"].append({"role": "user", "content": "WORK_SECRET"})
        self.state.update(work_mode="chat")
        captured = []
        def complete(_client, messages, **kwargs):
            captured.extend(messages)
            return {"content": "unique chat reply", "tool_calls": []}
        with patch.object(AgentLLMClient, "complete", complete):
            items = self.run_task(LLMConfig(model="test", work_mode="chat"))
        self.assertNotIn("WORK_SECRET", json.dumps(captured))
        self.assertEqual(self.agent.histories["work"][0]["content"], "WORK_SECRET")
        self.assertEqual(self.agent.histories["chat"][-1]["content"], "unique chat reply")
        self.assertEqual("".join(payload["text"] for kind, payload in items if kind == "token"),
                         "unique chat reply")
        self.assertFalse(any(kind == "chat" and payload[0] == "ClawX" for kind, payload in items))

    def test_task_uses_config_snapshot_and_chat_cannot_gain_tools(self):
        entered, release = threading.Event(), threading.Event()
        captured = []
        def complete(client, _messages, **_kwargs):
            entered.set()
            release.wait(timeout=3)
            captured.append(client.config.thinking_level)
            return {"content": "ok", "tool_calls": []}
        config = LLMConfig(model="test", work_mode="chat", thinking_level="High")
        self.state.update(work_mode="chat", thinking_level="High")
        with patch.object(AgentLLMClient, "complete", complete):
            self.agent.start("test", config)
            try:
                self.assertTrue(entered.wait(timeout=2))
                config.thinking_level = "Ultra"
                self.state.update(work_mode="work")
                with self.assertRaises(PolicyError):
                    self.agent._perform(Operation("write_file", {"path": "bad", "content": "x"}))
            finally:
                release.set()
                self.agent.thread.join(timeout=4)
        self.assertEqual(captured, ["High"])
        report = next(value for kind, value in list(self.events.queue) if kind == "deliverable")
        # 交付报告只列出输出文件，不展示回答。
        self.assertIn("本次对话没有读写任何文件", report)
        self.assertNotIn("ok", report)
        self.assertFalse((self.base / "bad").exists())

    def test_chat_styles_change_strategy_without_gaining_tools(self):
        self.state.update(work_mode="chat", thinking_level="Ultra")
        for style, level, prompt in (("instant", "High", "Instant Answer"),
                                     ("deep", "Ultra", "Deep Discussion")):
            with self.subTest(style=style):
                with patch.object(AgentLLMClient, "complete", return_value={"content": "reply", "tool_calls": []}) as call, \
                        patch("clawx.agent.ToolExecutor.execute") as execute:
                    items = self.run_task(LLMConfig(model="test", work_mode="chat",
                                                  thinking_level="Ultra", chat_style=style))
                self.assertIn(prompt, call.call_args.args[0][0]["content"])
                self.assertEqual(call.call_args.kwargs["tools"], False)
                execute.assert_not_called()
                self.assertFalse(any(kind == "thinking" for kind, _value in items))

    def test_execution_duration_uses_worker_clock_on_success_error_and_stop(self):
        for failure in (None, PolicyError("API HTTP 402"), Cancelled("已停止")):
            with self.subTest(failure=failure):
                clock = SimpleNamespace(monotonic=unittest.mock.Mock(side_effect=[100.0, 402.4]))
                with patch("clawx.agent.time", clock), \
                        patch.object(self.agent, "_react", return_value="完成", side_effect=failure):
                    items = self.run_task(LLMConfig(model="test", work_mode="work"))
                start = next(value for kind, value in items if kind == "execution_start")
                end = next(value for kind, value in items if kind == "execution_end")
                self.assertEqual(start, {"started": 100.0, "mode": "work"})
                self.assertAlmostEqual(end["elapsed"], 302.4)
                self.assertFalse(any(kind == "thinking" for kind, _value in items))


if __name__ == "__main__":
    unittest.main()
