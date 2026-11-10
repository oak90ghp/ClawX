from pathlib import Path
import json
import os
import queue
import tempfile
import threading
import time
import unittest
import uuid

from clawx.agent import AgentController
from clawx.core import (ApprovalBroker, Cancelled, ConversationManager, Operation, PolicyError,
                        PolicyState, ProjectManager, ScopeValidator, SecurityInterceptor,
                        SkillManager)
from clawx.llm import AgentLLMClient, LLMConfig
from clawx.tools import ToolExecutor, split_command


class ImmediateBroker:
    def __init__(self, answer=None):
        self.cancel = threading.Event()
        self.answer = answer
        self.requests = []

    def ask(self, request):
        self.requests.append(request)
        if self.answer is False:
            raise PolicyError("用户拒绝")
        return self.answer(request) if callable(self.answer) else request.editable


class SecurityTests(unittest.TestCase):
    def setUp(self):
        test_parent = Path(__file__).resolve().parent.parent / ".test-runs"
        test_parent.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=test_parent)
        self.base = Path(self.temp.name)
        if os.path.commonpath([str(test_parent), str(self.base.resolve())]) != str(test_parent):
            raise RuntimeError("Test cleanup target escaped test directory")
        self.root = self.base / "root"
        self.root.mkdir()
        self.state = PolicyState(str(self.root), tier=4)
        self.scope = ScopeValidator(self.state)
        self.skills = SkillManager(self.base / "skills.json")
        self.events = []
        self.broker = ImmediateBroker()
        self.audits = []

        def audit(details):
            self.audits.append(details)
            return "[SAFE]"

        self.security = SecurityInterceptor(self.state, self.scope, self.broker, audit, self.emit)
        self.executor = ToolExecutor(self.state, self.scope, self.security, self.skills,
                                     self.broker.cancel, self.emit)

    def tearDown(self):
        self.temp.cleanup()

    def emit(self, kind, payload):
        self.events.append((kind, payload))

    def write(self, name="a.txt", content="hello"):
        return Operation("write_file", {"path": name, "content": content})

    def test_traversal_and_sibling_prefix_blocked(self):
        for target in ("../escape.txt", str(self.base / "root-sibling" / "a")):
            with self.assertRaises(PolicyError):
                self.scope.resolve(target)

    def test_canonical_inside(self):
        self.assertEqual(self.scope.resolve("./a/../ok.txt"), str(self.root / "ok.txt"))

    def test_project_extra_roots_are_allowed_without_changing_relative_base(self):
        extra = self.base / "shared"
        extra.mkdir()
        self.state.update(allowed_roots=(str(extra),))
        self.assertEqual(self.scope.resolve("relative.txt"), str(self.root / "relative.txt"))
        self.assertEqual(self.scope.resolve(str(extra / "shared.txt")), str(extra / "shared.txt"))
        with self.assertRaisesRegex(PolicyError, "超出项目允许目录"):
            self.scope.resolve(str(self.base / "outside.txt"))

    def test_project_manager_persists_selection_prompt_and_roots(self):
        extra = self.base / "shared"
        second = self.base / "second"
        extra.mkdir()
        second.mkdir()
        path = self.base / "projects.json"
        manager = ProjectManager(path, self.root)
        original = manager.active()
        other = {"id": "1" * 32, "name": "文档项目", "root": str(second),
                 "prompt": "优先使用中文，并先验证结果。", "allowed_folders": [str(extra), str(second)]}
        manager.replace([original, other], other["id"])
        reopened = ProjectManager(path, self.root)
        self.assertEqual(reopened.active()["name"], "文档项目")
        self.assertEqual(reopened.active()["prompt"], "优先使用中文，并先验证结果。")
        self.assertEqual(reopened.active()["allowed_folders"], [str(extra.resolve())])
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["active_id"], other["id"])

    def test_project_manager_rejects_invalid_and_duplicate_projects(self):
        manager = ProjectManager(self.base / "projects.json", self.root)
        item = manager.active()
        duplicate = dict(item, id="2" * 32)
        with self.assertRaisesRegex(ValueError, "名称不能重复"):
            manager.replace([item, duplicate])
        with self.assertRaisesRegex(ValueError, "主目录必须"):
            manager.replace([dict(item, root=42)])
        with self.assertRaisesRegex(ValueError, "主目录不存在"):
            manager.replace([dict(item, root=str(self.base / "missing"))])

    def test_outside_cwd_blocked(self):
        with self.assertRaises(PolicyError):
            self.executor.execute(Operation("run_command", {"command": "pwd", "cwd": str(self.base)}))

    def test_whole_computer_relaxes_prefix(self):
        self.state.update(whole_computer=True)
        self.assertEqual(self.scope.resolve(str(self.base / "other.txt")), str(self.base / "other.txt"))

    @unittest.skipUnless(os.name == "nt", "Windows-specific devices")
    def test_windows_device_ads_and_network_blocked(self):
        for path in ("NUL", "COM1.txt", "foo:stream", "\\\\server\\share\\a", "\\\\?\\C:\\a"):
            with self.assertRaises(PolicyError, msg=path):
                self.scope.resolve(path)

    def test_symlink_escape(self):
        link = self.root / "linked"
        try:
            link.symlink_to(self.base, target_is_directory=True)
        except OSError:
            self.skipTest("Creating symlinks requires host privileges")
        with self.assertRaises(PolicyError):
            self.scope.resolve(str(link / "outside.txt"))

    def test_tier4_write_read_delete(self):
        self.executor.execute(self.write())
        result = self.executor.execute(Operation("read_file", {"path": "a.txt"}))
        self.assertEqual(result["content"], "hello")
        self.executor.execute(Operation("delete_file", {"path": "a.txt"}))
        self.assertFalse((self.root / "a.txt").exists())
        self.assertEqual([x["kind"] for x in self.executor.records], ["新建", "读取", "删除"])
        self.assertFalse(self.broker.requests)
        self.assertFalse(self.audits)

    def test_tier4_still_checks_scope(self):
        with self.assertRaises(PolicyError):
            self.executor.execute(self.write("../outside.txt"))
        self.assertFalse((self.base / "outside.txt").exists())

    def test_tier1_strict_safe_only_and_fail_closed(self):
        self.state.update(tier=1)
        for reply in ("[BLOCK] danger", "", "SAFE", "[SAFE] but [BLOCK]", "[SAFE]\nextra"):
            self.security.auditor = lambda _, value=reply: value
            with self.assertRaises(PolicyError):
                self.executor.execute(self.write())
        self.assertFalse((self.root / "a.txt").exists())
        self.security.auditor = lambda _: "[SAFE]"
        self.executor.execute(self.write())
        self.assertTrue((self.root / "a.txt").exists())

    def test_tier1_auditor_exception_prevents_write(self):
        self.state.update(tier=1)
        def unavailable(_details):
            raise PolicyError("API offline")
        self.security.auditor = unavailable
        with self.assertRaises(PolicyError):
            self.executor.execute(self.write())
        self.assertFalse((self.root / "a.txt").exists())

    def test_tier1_policy_change_during_audit_invalidates(self):
        self.state.update(tier=1)
        def changing(_details):
            self.state.update(tier=4)
            return "[SAFE]"
        self.security.auditor = changing
        with self.assertRaises(PolicyError):
            self.executor.execute(self.write())
        self.assertFalse((self.root / "a.txt").exists())

    def test_tier2_approval_and_diff(self):
        self.state.update(tier=2)
        (self.root / "a.txt").write_text("old", encoding="utf-8")
        self.executor.execute(self.write(content="new"))
        self.assertEqual(len(self.broker.requests), 1)
        self.assertIn("-old", self.broker.requests[0].text)
        self.assertIn("+new", self.broker.requests[0].text)

    def test_tier2_denial(self):
        self.state.update(tier=2)
        self.broker.answer = False
        with self.assertRaises(PolicyError):
            self.executor.execute(self.write())
        self.assertFalse((self.root / "a.txt").exists())

    def test_tier2_edited_content(self):
        self.state.update(tier=2)
        self.broker.answer = lambda request: json.dumps({"tool": "write_file", "arguments": {"path": "edited.txt", "content": "reviewed"}})
        self.executor.execute(self.write())
        self.assertEqual((self.root / "edited.txt").read_text(), "reviewed")
        self.assertFalse((self.root / "a.txt").exists())

    def test_tier2_edited_path_revalidated(self):
        self.state.update(tier=2)
        self.broker.answer = lambda request: json.dumps({"tool": "write_file", "arguments": {"path": "../outside.txt", "content": "x"}})
        with self.assertRaises(PolicyError):
            self.executor.execute(self.write())
        self.assertFalse((self.base / "outside.txt").exists())

    def test_policy_change_during_approval_invalidates(self):
        self.state.update(tier=2)
        def changed(request):
            self.state.update(tier=4)
            return request.editable
        self.broker.answer = changed
        with self.assertRaises(PolicyError):
            self.executor.execute(self.write())
        self.assertFalse((self.root / "a.txt").exists())

    def test_tier3_no_plan_blocks_even_read(self):
        self.state.update(tier=3)
        for op in (self.write(), Operation("list_files", {"path": "."})):
            with self.assertRaises(PolicyError):
                self.executor.execute(op)

    def test_tier3_locks_sequence_content_paths_and_replay(self):
        self.state.update(tier=3)
        first = self.executor.normalize(self.write())
        second = self.executor.normalize(Operation("run_command", {"command": "echo ok", "cwd": "."}))
        self.security.install_plan([first, second], self.state.revision)
        for wrong in (second, self.write("different.txt"), self.write(content="different")):
            with self.assertRaises(PolicyError):
                self.executor.execute(wrong)
        self.executor.execute(first)
        with self.assertRaises(PolicyError):
            self.executor.execute(first)
        result = self.executor.execute(second)
        self.assertEqual(result["returncode"], 0)
        with self.assertRaises(PolicyError):
            self.executor.execute(second)

    def test_tier3_locks_command_cwd(self):
        self.state.update(tier=3)
        (self.root / "sub").mkdir()
        op = self.executor.normalize(Operation("run_command", {"command": "pwd", "cwd": "."}))
        self.security.install_plan([op], self.state.revision)
        with self.assertRaises(PolicyError):
            self.executor.execute(Operation("run_command", {"command": "pwd", "cwd": "sub"}))

    def test_tier3_setting_change_expires_plan(self):
        self.state.update(tier=3)
        op = self.executor.normalize(self.write())
        self.security.install_plan([op], self.state.revision)
        self.state.update(tier=3)
        with self.assertRaises(PolicyError):
            self.executor.execute(op)

    def test_portable_shell_cannot_execute_user_code(self):
        with self.assertRaises(PolicyError):
            self.executor.execute(Operation("run_command", {"command": 'python -c "print(1)"', "cwd": "."}))
        result = self.executor.execute(Operation("run_command", {"command": "echo harmless && whoami", "cwd": "."}))
        self.assertIn("harmless && whoami", result["output"])
        self.assertEqual(result["returncode"], 0)

    def test_command_file_operand_escape_blocked(self):
        with self.assertRaises(PolicyError):
            self.executor.execute(Operation("run_command", {"command": 'cat "../secret.txt"', "cwd": "."}))

    def test_check_python_no_execution(self):
        script = self.root / "script.py"
        marker = self.root / "should_not_exist"
        script.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n", encoding="utf-8")
        result = self.executor.execute(Operation("run_command", {"command": 'check_python "script.py"', "cwd": "."}))
        self.assertEqual(result["returncode"], 0)
        self.assertIn("syntax OK", result["output"])
        self.assertFalse(marker.exists())
        self.assertFalse((self.root / "__pycache__").exists())

    def test_syntax_error_exit_and_stderr(self):
        (self.root / "bad.py").write_text("def invalid(\n", encoding="utf-8")
        result = self.executor.execute(Operation("run_command", {"command": 'check_python "bad.py"', "cwd": "."}))
        self.assertNotEqual(result["returncode"], 0)
        self.assertIn("[stderr]", result["output"])

    def test_command_preserves_windows_paths(self):
        self.assertEqual(split_command('cat "C:\\User Files\\a.txt"'), ["cat", "C:\\User Files\\a.txt"])

    def test_echo_roundtrip_quotes_and_backslashes(self):
        op = Operation("run_command", {"command": '''echo "C:\\User Files\\a.txt" "it's safe"''', "cwd": "."})
        normalized = self.executor.normalize(op)
        self.assertEqual(normalized.signature(), self.executor.normalize(normalized).signature())
        result = self.executor.execute(op)
        self.assertIn("C:\\User Files\\a.txt it's safe", result["output"])

    def test_unicode_command_output(self):
        result = self.executor.execute(Operation("run_command", {"command": 'echo "你好 ClawX"', "cwd": "."}))
        self.assertIn("你好 ClawX", result["output"])

    def test_skills_roundtrip_intersection(self):
        self.skills.replace([{"name": "safe", "description": "d", "instruction": "i",
                              "enabled": True, "allowed_tools": ["read_file"]}])
        reopened = SkillManager(self.base / "skills.json")
        self.assertEqual(reopened.all()[0]["name"], "safe")
        with self.assertRaises(PolicyError):
            self.executor.execute(self.write())
        items = self.skills.all()
        items[0]["enabled"] = False
        self.skills.replace(items)
        self.executor.execute(self.write())

    def test_cancellation_approval_wait(self):
        events, cancel = queue.Queue(), threading.Event()
        broker = ApprovalBroker(events, cancel)
        from clawx.core import ApprovalRequest
        request = ApprovalRequest("operation", "test", "", "{}", 0)
        caught = []
        def waiting():
            try:
                broker.ask(request)
            except Cancelled:
                caught.append(True)
        worker = threading.Thread(target=waiting)
        worker.start()
        events.get(timeout=1)
        cancel.set()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(caught, [True])

    def test_cancellation_terminates_native_command(self):
        self.state.update(whole_computer=True)
        command = "Start-Sleep -Seconds 20" if os.name == "nt" else "sleep 20"
        caught = []
        def running():
            try:
                self.executor.execute(Operation("run_command", {"command": command, "cwd": "."}))
            except Cancelled:
                caught.append(True)
        worker = threading.Thread(target=running)
        worker.start()
        time.sleep(0.5)
        self.broker.cancel.set()
        worker.join(timeout=8)
        self.assertFalse(worker.is_alive())
        self.assertEqual(caught, [True])

    def test_worker_generates_report_with_real_client(self):
        from unittest.mock import patch
        script = os.path.join(str(self.root), "worker_demo.py")
        report_path = os.path.join(str(self.root), "worker_demo_report.md")
        operations = [("write_file", {"path": script, "content": "print('hi')\n"}),
                      ("read_file", {"path": script}),
                      ("write_file", {"path": report_path, "content": "# 报告\n"}),
                      ("run_command", {"command": "pwd", "cwd": str(self.root)})]
        pending = list(operations)
        def complete(_client, _messages, **_kwargs):
            if pending:
                tool, args = pending.pop(0)
                return {"content": "", "tool_calls": [
                    {"id": "c1", "type": "function",
                     "function": {"name": tool, "arguments": json.dumps(args)}}]}
            return {"content": "完成", "tool_calls": []}
        events = queue.Queue()
        controller = AgentController(self.state, self.skills, events)
        with patch.object(AgentLLMClient, "complete", complete):
            controller.start("生成示例文件", LLMConfig(model="test", api_key="k"))
            controller.thread.join(timeout=15)
        self.assertFalse(controller.running)
        items = []
        while not events.empty():
            items.append(events.get())
        reports = [x for kind, x in items if kind == "deliverable"]
        self.assertEqual(len(reports), 1)
        self.assertIn("状态：完成", reports[0])
        self.assertIn("worker_demo.py", reports[0])
        self.assertTrue(Path(script).exists())
        self.assertTrue(Path(report_path).exists())

    def test_chat_mode_blocks_tool_execution_and_runs_pure_conversation(self):
        from unittest.mock import patch
        self.state.update(work_mode="chat", thinking_level="Ultra")
        with self.assertRaisesRegex(PolicyError, "标准聊天"):
            self.executor.execute(self.write())
        events = queue.Queue()
        controller = AgentController(self.state, self.skills, events)
        with patch.object(AgentLLMClient, "complete",
                          return_value={"content": "架构解耦的解释", "tool_calls": []}):
            controller.start("解释一下什么是架构解耦",
                             LLMConfig(model="test", api_key="k", work_mode="chat",
                                       thinking_level="Ultra", chat_style="deep"))
            controller.thread.join(timeout=10)
        items = []
        while not events.empty():
            items.append(events.get())
        thinking_events = [payload for kind, payload in items if kind == "thinking"]
        self.assertEqual(thinking_events, [])
        reports = [x for kind, x in items if kind == "deliverable"]
        self.assertEqual(len(reports), 1)
        # 交付报告只列出输出文件，不展示回答内容。
        self.assertIn("本次对话没有读写任何文件", reports[0])
        self.assertNotIn("思考等级", reports[0])
        self.assertNotIn("架构解耦", reports[0])


class ConversationManagerTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parent.parent / ".test-runs"
        parent.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.path = Path(self.temp.name) / "conversations.json"

    def tearDown(self):
        self.temp.cleanup()

    def record(self, **overrides):
        item = {"id": uuid.uuid4().hex, "project_id": "p1", "title": "你好",
                "created": "2026-01-01T00:00:00", "updated": "2026-01-01T00:00:01",
                "messages": [{"role": "user", "content": "hi", "mode": "chat"}]}
        item.update(overrides)
        return item

    def test_upsert_list_delete_and_validation(self):
        manager = ConversationManager(self.path)
        first = self.record()
        manager.upsert(first)
        self.assertEqual(len(manager.all("p1")), 1)
        self.assertEqual(manager.all("p2"), [])
        updated = self.record(id=first["id"], title="改过", updated="2026-01-01T00:00:02",
                              messages=[{"role": "user", "content": "hi", "mode": "chat"},
                                        {"role": "assistant", "content": "ok", "mode": "chat"}])
        manager.upsert(updated)
        self.assertEqual(len(manager.all("p1")), 1)
        self.assertEqual(manager.by_id(first["id"])["title"], "改过")
        with self.assertRaises(ValueError):
            manager.upsert(self.record(messages=[{"role": "system", "content": "x", "mode": "chat"}]))
        with self.assertRaises(ValueError):
            manager.upsert(self.record(messages=[{"role": "user", "content": "x", "mode": "cowork"}]))
        manager.delete(first["id"])
        self.assertEqual(manager.all(), [])

    def test_reload_from_disk_preserves_records(self):
        manager = ConversationManager(self.path)
        record = self.record()
        manager.upsert(record)
        reloaded = ConversationManager(self.path)
        self.assertEqual(reloaded.by_id(record["id"])["title"], "你好")

    def test_fork_copies_only_selected_prefix_and_keeps_source_independent(self):
        manager = ConversationManager(self.path)
        messages = [{"role": role, "content": str(index), "mode": "chat"}
                    for index, role in enumerate(("user", "assistant", "user", "assistant"))]
        source = self.record(messages=messages)
        manager.upsert(source)
        branch = manager.fork(source["id"], 1, "另一种方案")
        self.assertNotEqual(source["id"], branch["id"])
        self.assertEqual(branch["messages"], messages[:2])
        self.assertEqual(branch["project_id"], source["project_id"])
        self.assertEqual(branch["parent_id"], source["id"])
        self.assertEqual(branch["branch_at"], 1)
        branch["messages"][0]["content"] = "changed"
        manager.upsert(branch)
        self.assertEqual(manager.by_id(source["id"]), source)
        reloaded = ConversationManager(self.path)
        self.assertEqual(reloaded.by_id(branch["id"]), branch)
        manager.delete(source["id"])
        self.assertEqual(manager.by_id(branch["id"]), branch)

    def test_fork_validates_index_title_and_metadata_without_creating_records(self):
        manager = ConversationManager(self.path)
        source = self.record()
        manager.upsert(source)
        for index in (-1, 1, True, "0"):
            with self.subTest(index=index), self.assertRaises(ValueError):
                manager.fork(source["id"], index)
        for title in ("", " ", "x" * 81):
            with self.subTest(title=title), self.assertRaises(ValueError):
                manager.fork(source["id"], 0, title)
        with self.assertRaises(ValueError):
            manager.fork(uuid.uuid4().hex, 0)
        with self.assertRaises(ValueError):
            manager.upsert({**source, "parent_id": source["id"], "branch_at": 0})
        with self.assertRaises(ValueError):
            manager.upsert({**source, "parent_id": uuid.uuid4().hex})
        self.assertEqual(manager.all(), [source])


if __name__ == "__main__":
    unittest.main()
