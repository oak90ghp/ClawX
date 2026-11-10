from pathlib import Path
import gc
import json
import os
import tempfile
import threading
import time
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

try:
    import tkinter as tk
    from tkinter import ttk
    from clawx.ui import BranchDialog, ClawXApp, ConfigDialog, ProjectDialog, SkillDialog, TIERS
    from clawx.llm import AgentLLMClient
    from clawx.widgets import THINKING_COLORS, ThinkingMenuButton
    TK_AVAILABLE = True
except ModuleNotFoundError:
    TK_AVAILABLE = False


@unittest.skipUnless(TK_AVAILABLE, "tkinter is not installed in this Python runtime")
class UITests(unittest.TestCase):
    def setUp(self):
        # Tk interpreters from earlier tests must be finalized on the main thread,
        # before new agent threads can trigger Python's cyclic garbage collector.
        gc.collect()
        parent = Path(__file__).resolve().parent.parent / ".test-runs"
        parent.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        if os.path.commonpath([str(parent), str(Path(self.temp.name).resolve())]) != str(parent):
            raise RuntimeError("Unsafe test cleanup path")
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.temp.cleanup()
            self.skipTest("No graphical display: " + str(exc))
        self.root.withdraw()
        self.hotkey_patch = patch("clawx.ui.GlobalHotkey.start", return_value=False)
        self.hotkey_patch.start()
        self.addCleanup(self.hotkey_patch.stop)
        self.app = ClawXApp(self.root, Path(self.temp.name))
        self.root.update()

    def tearDown(self):
        self.app.agent.stop()
        if self.app.review:
            self.app.review.finish(False)
        if self.app.agent.thread:
            self.app.agent.thread.join(timeout=8)
        self.app.close()
        self.app = None
        self.root = None
        gc.collect()
        self.temp.cleanup()

    def wait_task(self, approve=True, timeout=12):
        deadline = time.monotonic() + timeout
        approvals = 0
        while time.monotonic() < deadline:
            self.root.update()
            if self.app.review:
                request = self.app.review.request
                self.app.review.finish(approve, request.editable if approve else "")
                approvals += 1
            if self.app.report and not self.app.agent.running and not self.app.ui_busy:
                self.root.update()
                return approvals
            time.sleep(0.01)
        self.fail("UI task did not complete")

    def scripted(self, operations, final="任务完成。"):
        """模拟真实模型：按顺序返回工具调用，最后返回纯文本回复。"""
        pending = list(operations)

        def complete(_client, _messages, **_kwargs):
            if pending:
                tool, args = pending.pop(0)
                return {"content": "", "tool_calls": [
                    {"id": "call", "type": "function",
                     "function": {"name": tool, "arguments": json.dumps(args, ensure_ascii=False)}}]}
            return {"content": final, "tool_calls": []}
        return complete

    def real_config(self):
        self.app.config.model = "test-model"
        self.app.config.api_key = "test-key"

    def test_dashboard_widgets_and_dialogs(self):
        self.assertEqual(self.app.state.tier, 2)
        self.assertEqual(self.app.state.work_mode, "work")
        self.assertEqual(self.app.state.thinking_level, "Medium")
        def descend(parent):
            result = []
            for child in parent.winfo_children():
                result.append(child)
                result.extend(descend(child))
            return result
        all_widgets = descend(self.root)
        self.assertEqual(len([w for w in all_widgets if isinstance(w, ttk.Panedwindow)]), 3)
        self.assertEqual(len(self.app.workspace_panes.panes()), 2)
        self.assertEqual(len(self.app.center_panes.panes()), 1)
        self.assertEqual(self.app.terminal_panel.winfo_manager(), "")
        self.assertEqual(len(self.app.inspector_panes.panes()), 2)
        self.assertEqual(set(self.app.nav_buttons), {"chat", "files", "delivery"})
        self.assertEqual(set(self.app.mode_buttons), {"work", "chat"})
        self.assertEqual([button.cget("text") for button in self.app.mode_buttons.values()], ["聊天", "开发"])
        self.assertEqual([button.cget("text") for button in self.app.chat_style_buttons.values()], ["即时回答", "深入讨论"])
        self.assertEqual(self.app.thinking_slider.level, "Medium")
        SkillDialog(self.app).destroy()
        ProjectDialog(self.app).destroy()
        ConfigDialog(self.app).destroy()

    def test_work_and_chat_mode_switching_and_ultra_effects(self):
        self.app.set_thinking_level("Ultra")
        self.assertEqual(self.app.state.thinking_level, "Ultra")
        self.assertFalse(hasattr(self.app, "window_border_glow"))
        self.assertEqual(self.app.thinking_controls.winfo_manager(), "")
        self.assertFalse(hasattr(self.app, "header_ultra_bar"))
        self.app.set_thinking_level("High")
        self.assertFalse(hasattr(self.app, "window_border_glow"))
        self.assertFalse(hasattr(self.app.composer, "_top_rays"))
        self.app.set_work_mode("chat")
        self.assertEqual(self.app.state.work_mode, "chat")
        self.real_config()
        self.app.input.insert("1.0", "标准聊天模式测试")
        with patch.object(AgentLLMClient, "complete",
                          return_value={"content": "聊天回复", "tool_calls": []}):
            self.app.send()
            approvals = self.wait_task()
        self.assertEqual(approvals, 0)
        self.assertNotIn("运行策略", self.app.chat_text.get("1.0", "end"))
        # 交付报告只列出输出文件，不展示回答。
        self.assertIn("本次对话没有读写任何文件", self.app.report)
        self.assertNotIn("标准聊天模式测试", self.app.report)
        self.assertFalse(list(Path(self.app.state.root).glob("*.py")))

    def test_sidebar_and_prompt_shortcuts(self):
        self.app.toggle_sidebar()
        self.assertFalse(self.app.sidebar_visible)
        self.app.toggle_sidebar()
        self.assertTrue(self.app.sidebar_visible)
        self.app.prefill("演示完整流程")
        self.assertEqual(self.app.input.get("1.0", "end-1c"), "演示完整流程")
        self.app.focus_panel("terminal")
        self.assertEqual(self.app.nav_selection, "chat")

    def test_project_dialog_saves_prompt_scope_and_switches_context(self):
        project_root = Path(self.temp.name) / "project-two"
        shared = Path(self.temp.name) / "shared"
        project_root.mkdir()
        shared.mkdir()
        self.app.agent.histories["work"].append({"role": "user", "content": "old project"})
        dialog = ProjectDialog(self.app)
        dialog.new()
        dialog.name.set("第二项目")
        dialog.root_path.set(str(project_root))
        dialog.folders.insert("end", str(shared))
        dialog.prompt.insert("1.0", "回答前先检查项目约束。")
        dialog.save_and_activate()
        self.root.update()
        active = self.app.projects.active()
        self.assertEqual(active["name"], "第二项目")
        self.assertEqual(active["prompt"], "回答前先检查项目约束。")
        self.assertEqual(self.app.state.project_name, "第二项目")
        self.assertEqual(self.app.workspace_name.cget("text"), "第二项目")
        self.assertEqual(self.app.agent.histories, {"work": [], "chat": []})
        self.assertEqual(self.app.state.allowed_roots, (str(shared.resolve()),))
        from clawx.core import ScopeValidator
        self.assertEqual(ScopeValidator(self.app.state).resolve(str(shared / "ok.txt")),
                         str(shared / "ok.txt"))
        data = json.loads((self.app.data_dir / "projects.json").read_text(encoding="utf-8"))
        self.assertNotIn("api_key", json.dumps(data))

    def test_new_task_clears_session_but_preserves_files(self):
        marker = Path(self.app.state.root) / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        self.app.agent.history.append({"role": "user", "content": "prior task"})
        self.app.chat("用户", "prior task")
        self.app.report = "prior report"
        self.app.new_task()
        self.assertEqual(self.app.agent.history, [])
        self.assertEqual(self.app.report, "")
        self.assertEqual(self.app.chat_text.get("1.0", "end-1c"), "")
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_work_tier2_modal_approval_flow(self):
        self.real_config()
        root = self.app.state.root
        script = os.path.join(root, "demo.py")
        report = os.path.join(root, "demo_report.md")
        ops = [("write_file", {"path": script, "content": "print('hi')\n"}),
               ("read_file", {"path": script}),
               ("write_file", {"path": script, "content": "print('hi again')\n"}),
               ("run_command", {"command": "pwd", "cwd": root}),
               ("run_command", {"command": 'check_python "demo.py"', "cwd": root}),
               ("write_file", {"path": report, "content": "# 报告\n"})]
        self.app.input.insert("1.0", "创建演示脚本")
        with patch.object(AgentLLMClient, "complete", self.scripted(ops)):
            self.app.send()
            approvals = self.wait_task()
        # 首轮计划审批 1 次 + 写/命令逐项审批（2 写 + 2 命令 + 1 写 = 5）。
        self.assertEqual(approvals, 6)
        self.assertIn("状态：完成", self.app.report)
        self.assertIn("demo.py", self.app.report)
        self.assertIn("[EXIT 0]", self.app.console.get("1.0", "end"))

    def test_work_tier3_one_plan_dialog(self):
        self.real_config()
        script = os.path.join(self.app.state.root, "plan_demo.py")
        plan = {"summary": "创建演示脚本",
                "steps": [{"tool": "write_file",
                           "arguments": {"path": script, "content": "print(1)\n"},
                           "reason": "演示"}]}
        self.app.tier.set(TIERS[2])
        self.app.change_tier()
        self.app.input.insert("1.0", "创建计划文件")
        with patch.object(AgentLLMClient, "complete",
                          return_value={"content": json.dumps(plan, ensure_ascii=False), "tool_calls": []}):
            self.app.send()
            approvals = self.wait_task()
        self.assertEqual(approvals, 1)
        self.assertIn("状态：完成", self.app.report)
        self.assertTrue(Path(script).exists())
        self.assertEqual(self.app.agent.executor.security.plan_revision, -1)

    def test_work_tier1_blocks_delete_without_dialog(self):
        self.real_config()
        scratch = os.path.join(self.app.state.root, "demo_scratch.txt")
        pending = [("write_file", {"path": scratch, "content": "temp\n"}),
                   ("delete_file", {"path": scratch})]

        def complete(_client, messages, **_kwargs):
            if _kwargs.get("audit"):
                details = json.dumps(messages, ensure_ascii=False)
                return {"content": "[BLOCK] 拦截删除" if "delete_file" in details else "[SAFE]",
                        "tool_calls": []}
            if pending:
                tool, args = pending.pop(0)
                return {"content": "", "tool_calls": [
                    {"id": "call", "type": "function",
                     "function": {"name": tool, "arguments": json.dumps(args, ensure_ascii=False)}}]}
            return {"content": "完成", "tool_calls": []}

        self.app.tier.set(TIERS[0])
        self.app.change_tier()
        self.app.input.insert("1.0", "演示删除")
        with patch.object(AgentLLMClient, "complete", complete):
            self.app.send()
            self.assertEqual(self.wait_task(), 0)
        self.assertTrue(Path(scratch).exists())
        self.assertIn("包含拒绝或失败", self.app.report)

    def test_work_tier4_blocks_traversal_without_approval(self):
        self.real_config()
        root = self.app.state.root
        script = os.path.join(root, "demo_t4.py")
        escape = os.path.join(root, "..", "demo_escape.txt")
        ops = [("write_file", {"path": script, "content": "print(1)\n"}),
               ("write_file", {"path": escape, "content": "probe"})]
        self.app.tier.set(TIERS[3])
        with patch("clawx.ui.messagebox.askyesno", return_value=True):
            self.app.change_tier()
        self.app.input.insert("1.0", "演示越界")
        with patch.object(AgentLLMClient, "complete", self.scripted(ops)):
            self.app.send()
            self.assertEqual(self.wait_task(), 0)
        self.assertTrue(Path(script).exists())
        self.assertFalse(Path(escape).exists())
        self.assertIn("包含拒绝或失败", self.app.report)

    def test_stop_pending_approval(self):
        self.real_config()
        script = os.path.join(self.app.state.root, "stop_demo.py")
        ops = [("write_file", {"path": script, "content": "print(1)\n"})]
        self.app.input.insert("1.0", "创建文件")
        with patch.object(AgentLLMClient, "complete", self.scripted(ops)):
            self.app.send()
            deadline = time.monotonic() + 5
            while not self.app.review and time.monotonic() < deadline:
                self.root.update()
                time.sleep(0.01)
            self.assertIsNotNone(self.app.review)
            self.app.stop()
            self.wait_task()
        self.assertIn("状态：已停止", self.app.report)
        self.assertFalse(Path(script).exists())

    def test_running_task_locks_mode_and_reasoning_until_done(self):
        self.real_config()
        entered, release = threading.Event(), threading.Event()
        def complete(_client, _messages, **_kwargs):
            entered.set()
            release.wait(timeout=5)
            return {"content": "完成", "tool_calls": []}
        self.app.prefill("运行任务")
        with patch.object(AgentLLMClient, "complete", complete):
            self.app.send()
            self.assertTrue(entered.wait(timeout=2))
            self.app.set_work_mode("chat")
            self.app.set_thinking_level("Ultra")
            self.assertEqual(self.app.state.work_mode, "work")
            self.assertEqual(self.app.config.thinking_level, "Medium")
            self.assertFalse(self.app.thinking_slider.enabled)
            self.app.set_chat_style("deep")
            self.assertEqual(self.app.config.chat_style, "instant")
            release.set()
            self.wait_task()
        self.assertTrue(self.app.thinking_slider.enabled)
        self.assertEqual(str(self.app.mode_buttons["chat"].cget("state")), "normal")

    def test_mode_history_switch_and_preferences_do_not_store_key(self):
        self.app.agent.histories["work"].extend([
            {"role": "user", "content": "work context"}, {"role": "assistant", "content": "work answer"}])
        self.app.set_work_mode("chat")
        self.assertNotIn("work context", self.app.chat_text.get("1.0", "end"))
        self.app.agent.histories["chat"].append({"role": "user", "content": "chat context"})
        self.app.set_work_mode("work")
        self.assertIn("work context", self.app.chat_text.get("1.0", "end"))
        self.app.config.api_key = "private-test-key"
        self.app.set_thinking_level("Ultra")
        data = json.loads((self.app.data_dir / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(data["thinking_level"], "Ultra")
        self.assertEqual(data["work_mode"], "work")
        self.assertNotIn("api_key", data)
        self.app.new_task()
        self.assertEqual(self.app.agent.histories["work"], [])
        self.assertTrue(self.app.agent.histories["chat"])

    def test_thinking_menu_has_no_marquee_border_effects(self):
        from tkinter import ttk
        original = self.app.sidebar.cget("background")
        original_tree = ttk.Style(self.root).lookup("Treeview", "background")
        original_shape = self.app.composer.itemcget(self.app.composer.shape, "fill")
        self.app.set_thinking_level("Ultra")
        self.assertFalse(hasattr(self.app, "window_border_glow"))
        self.assertFalse(hasattr(self.app, "thinking_menu_glow"))
        self.app.toggle_thinking_menu()
        self.assertTrue(self.app.thinking_menu_open)
        self.assertEqual(self.app.thinking_controls.winfo_manager(), "place")
        self.assertEqual(self.app.sidebar.cget("background"), original)
        self.assertEqual(ttk.Style(self.root).lookup("Treeview", "background"), original_tree)
        self.assertEqual(self.app.composer.itemcget(self.app.composer.shape, "fill"), original_shape)
        self.app.toggle_thinking_menu()
        self.assertFalse(self.app.thinking_menu_open)
        self.assertEqual(self.app.thinking_controls.winfo_manager(), "")
        self.assertEqual(self.app.sidebar.cget("background"), original)
        self.assertEqual(ttk.Style(self.root).lookup("Treeview", "background"), original_tree)
        self.assertEqual(self.app.composer.itemcget(self.app.composer.shape, "fill"), original_shape)
        self.assertEqual(self.app.state.thinking_level, "Ultra")
        self.app.toggle_thinking_menu()
        self.app.set_thinking_level("Low")
        self.assertTrue(self.app.thinking_menu_open)
        self.app._escape()
        self.assertFalse(self.app.thinking_menu_open)

    def test_composer_thinking_label_uses_level_color_and_ultra_gradient(self):
        button = self.app.thinking_menu_button
        self.app.set_thinking_level("Medium")
        fills = [button.itemcget(item, "fill") for item in button.find_all()
                 if button.type(item) == "text"]
        self.assertIn(THINKING_COLORS["Medium"], fills)
        self.app.set_thinking_level("Ultra")
        fills = [button.itemcget(item, "fill") for item in button.find_all()
                 if button.type(item) == "text"]
        for color in ThinkingMenuButton.ULTRA_TEXT:
            self.assertIn(color, fills)

    def test_real_connection_button_uses_background_probe(self):
        dialog = ConfigDialog(self.app)
        dialog.fields["model"].set("custom-model")
        dialog.fields["api_key"].set("k")
        with patch("clawx.ui.AgentLLMClient.probe", return_value="模型在线") as probe:
            dialog.test_connection()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and not self.app.events.qsize():
                time.sleep(0.01)
            self.app.pump()
        probe.assert_called_once()
        self.assertIn("连接成功", dialog.test_status.get())
        self.assertEqual(str(dialog.test_button.cget("state")), "normal")
        dialog.destroy()

    def test_no_marquee_animation_classes_or_widgets_exist(self):
        import clawx.widgets as widget_module
        self.assertFalse(hasattr(widget_module, "WindowBorderGlow"))
        self.assertFalse(hasattr(widget_module, "PanelBorderGlow"))
        self.app.set_thinking_level("Ultra")
        self.app.toggle_thinking_menu()
        self.root.update()
        self.assertTrue(self.app.thinking_menu_open)
        self.assertEqual([w for w in self.root.winfo_children() if isinstance(w, tk.Canvas)], [])

    def test_full_allow_risk_cancel_preserves_policy_and_selection(self):
        revision = self.app.state.revision
        self.app.tier.set(TIERS[3])
        with patch("clawx.ui.messagebox.askyesno", return_value=False) as prompt:
            self.app.change_tier()
        self.assertEqual(self.app.state.tier, 2)
        self.assertEqual(self.app.state.revision, revision)
        self.assertEqual(self.app.tier.get(), TIERS[1])
        self.assertEqual(prompt.call_args.kwargs["default"], "no")
        self.assertEqual(prompt.call_args.kwargs["icon"], "warning")
        self.assertIn("数据丢失", prompt.call_args.args[1])
        self.assertIn("整个计算机", prompt.call_args.args[1])

    def test_full_allow_requires_confirmation_before_policy_change(self):
        def accept(*_args, **_kwargs):
            self.assertEqual(self.app.state.tier, 2)
            return True
        self.app.tier.set(TIERS[3])
        with patch("clawx.ui.messagebox.askyesno", side_effect=accept) as prompt:
            self.app.change_tier()
        prompt.assert_called_once()
        self.assertEqual(self.app.state.tier, 4)
        with patch("clawx.ui.messagebox.askyesno") as prompt:
            self.app.change_tier()
            self.app.tier.set(TIERS[1])
            self.app.change_tier()
        prompt.assert_not_called()
        self.assertEqual(self.app.state.tier, 2)

    def test_submenu_dismisses_on_outside_click_resize_and_send(self):
        from types import SimpleNamespace
        self.app.set_thinking_level("Ultra")
        self.app.toggle_thinking_menu()
        self.app._dismiss_thinking_menu(SimpleNamespace(widget=self.app.thinking_slider))
        self.assertTrue(self.app.thinking_menu_open)
        self.app._dismiss_thinking_menu(SimpleNamespace(widget=self.app.input))
        self.assertFalse(self.app.thinking_menu_open)
        self.app.toggle_thinking_menu()
        self.app._thinking_window_resize(SimpleNamespace(widget=self.root))
        self.assertFalse(self.app.thinking_menu_open)
        self.app.toggle_thinking_menu()
        self.app.prefill("演示完整流程")
        self.real_config()
        with patch.object(AgentLLMClient, "complete",
                          return_value={"content": "完成", "tool_calls": []}):
            self.app.send()
        self.assertFalse(self.app.thinking_menu_open)
        self.wait_task()

    def test_instant_and_deep_preserve_level_history_and_ultra(self):
        from clawx.core import INSTANT_LEVELS, INSTANT_LABELS
        self.app.set_thinking_level("Ultra")
        self.app.set_work_mode("chat")
        # 即时回答把 Ultra 钳制为 High，且滑块保留三档 标准/中/高。
        self.assertEqual(self.app.config.effective_level(), "High")
        self.assertFalse(hasattr(self.app, "window_border_glow"))
        self.assertTrue(self.app.thinking_slider.enabled)
        self.assertEqual(self.app.thinking_slider.levels, INSTANT_LEVELS)
        self.assertEqual(self.app.thinking_slider.labels, INSTANT_LABELS)
        self.assertEqual(self.app.thinking_controls.winfo_manager(), "")
        self.assertEqual(self.app.chat_style_switch.winfo_manager(), "pack")
        self.app.agent.histories["chat"].append({"role": "user", "content": "shared conversation"})
        self.app.set_chat_style("deep")
        self.assertEqual(self.app.config.effective_level(), "Ultra")
        self.assertFalse(hasattr(self.app, "window_border_glow"))
        self.assertEqual(self.app.thinking_controls.winfo_manager(), "")
        self.assertTrue(self.app.thinking_slider.enabled)
        self.assertEqual(self.app.agent.histories["chat"][0]["content"], "shared conversation")
        self.app.set_chat_style("instant")
        self.assertEqual(self.app.state.thinking_level, "Ultra")
        # Ctrl+U 在即时回答内于 高/中 之间切换，不再改变聊天方式。
        self.app.toggle_ultra_thinking()
        self.assertEqual(self.app.config.chat_style, "instant")
        self.assertEqual(self.app.config.thinking_level, "Medium")
        self.assertFalse(hasattr(self.app, "window_border_glow"))
        self.app.set_work_mode("work")
        self.assertEqual(self.app.chat_style_switch.winfo_manager(), "")
        data = json.loads((self.app.data_dir / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(data["chat_style"], "instant")
        self.assertEqual(data["thinking_level"], "Medium")

    def test_slider_drag_commit_keyboard_bounds_and_disabled_input(self):
        from types import SimpleNamespace
        slider = self.app.thinking_slider
        calls = []
        slider.command = calls.append
        left, right = slider._bounds()
        slider._press(SimpleNamespace(x=left))
        slider._drag(SimpleNamespace(x=right))
        self.assertEqual(calls, [])
        self.assertEqual(slider.level, "Ultra")
        slider._release(SimpleNamespace(x=right))
        self.assertEqual(calls, ["Ultra"])
        slider._key(1)
        self.assertEqual(slider.level, "Ultra")
        slider._choose(0)
        slider._key(-1)
        self.assertEqual(slider.level, "Low")
        slider._press(SimpleNamespace(x=right))
        slider._cancel_drag()
        self.assertEqual(slider.level, "Low")
        slider._press(SimpleNamespace(x=right))
        slider.set_enabled(False)
        self.assertEqual(slider.level, "Low")
        self.assertFalse(slider._dragging)
        count = len(calls)
        slider._press(SimpleNamespace(x=right))
        slider._release(SimpleNamespace(x=right))
        slider._key(1)
        self.assertEqual(slider.level, "Low")
        self.assertEqual(len(calls), count)

    def test_legacy_preferences_migrate_and_new_style_survives_restart(self):
        self.app.close()
        path = self.app.data_dir / "settings.json"
        path.write_text(json.dumps({"work_mode": "chat", "thinking_level": "Ultra"}), encoding="utf-8")
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = ClawXApp(self.root, Path(self.temp.name))
        self.assertEqual(self.app.config.chat_style, "deep")
        self.assertFalse(hasattr(self.app, "window_border_glow"))
        self.app.set_chat_style("instant")
        self.app.close()
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = ClawXApp(self.root, Path(self.temp.name))
        self.assertEqual(self.app.config.chat_style, "instant")
        self.assertEqual(self.app.config.thinking_level, "Ultra")
        self.assertFalse(hasattr(self.app, "window_border_glow"))

    def test_model_registry_and_selection_clamps_levels(self):
        self.app.set_thinking_level("Ultra")
        entries = self.app.models.all()
        new_entry = {"id": uuid.uuid4().hex, "label": "轻量模型", "model": "light-model",
                     "provider": "openai", "base_url": "https://api.openai.com/v1",
                     "api_key": "test-key", "reasoning_api": "portable", "effort_ceiling": "high",
                     "levels": ["Low", "Medium"]}
        self.app.models.replace(entries + [new_entry], entries[0]["id"])
        self.app.select_model(new_entry["id"])
        self.assertEqual(self.app.config.model, "light-model")
        self.assertEqual(self.app.config.thinking_level, "Medium")
        self.assertEqual(self.app.thinking_slider.levels, ["Low", "Medium"])
        self.assertEqual(self.app.model_combo.get(), "轻量模型")
        saved = json.loads((self.app.data_dir / "models.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["active_id"], new_entry["id"])
        # 模型密钥保存在 models.json，settings.json 不保存密钥。
        self.assertIn("test-key", json.dumps(saved))
        self.assertNotIn("api_key", json.loads(
            (self.app.data_dir / "settings.json").read_text(encoding="utf-8")))

    def test_model_menu_combo_switches_model(self):
        entries = self.app.models.all()
        new_entry = {"id": uuid.uuid4().hex, "label": "备用模型", "model": "backup-model",
                     "provider": "anthropic", "base_url": "https://api.anthropic.com/v1",
                     "api_key": "k", "reasoning_api": "portable", "effort_ceiling": "medium",
                     "levels": ["Low", "Medium", "High"]}
        self.app.models.replace(entries + [new_entry], entries[0]["id"])
        self.app.toggle_thinking_menu()
        self.app.model_var.set("备用模型")
        self.app._model_combo_changed()
        self.assertEqual(self.app.config.model, "backup-model")
        self.assertEqual(self.app.config.provider, "anthropic")
        self.assertEqual(self.app.thinking_slider.levels, ["Low", "Medium", "High"])

    def test_theme_switch_preserves_content_and_persists(self):
        from clawx.widgets import LIGHT_PALETTE, theme as theme_module
        self.app.chat("ClawX", "**加粗** `code`")
        self.app.report = "# 交付\n\n状态：完成"
        self.app.set_theme("light")
        self.assertEqual(theme_module.mode, "light")
        self.assertEqual(self.app.shell.cget("background"), LIGHT_PALETTE["bg"])
        self.assertIn("加粗", self.app.chat_text.get("1.0", "end"))
        self.assertIn("code", self.app.chat_text.get("1.0", "end"))
        self.assertIn("状态：完成", self.app.delivery.get("1.0", "end"))
        data = json.loads((self.app.data_dir / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(data["theme"], "light")
        self.app.set_theme("dark")
        self.assertEqual(theme_module.mode, "dark")

    def test_theme_system_follows_os_preference(self):
        from clawx.widgets import theme as theme_module
        with patch.object(ClawXApp, "_system_prefers_light", return_value=True):
            self.app.set_theme("system")
        self.assertEqual(self.app.config.theme, "system")
        self.assertEqual(theme_module.mode, "light")
        self.app.set_theme("dark")

    def test_markdown_rendering_styles_headers_and_code(self):
        self.app.chat("ClawX", "### 标题\n**加粗** `内联`\n\n```python\nprint(1)\n```")
        content = self.app.chat_text.get("1.0", "end")
        self.assertIn("print(1)", content)
        self.assertIn("加粗", content)
        self.assertIn("md_code", self.app.chat_text.tag_names())
        self.assertIn("md_h3", self.app.chat_text.tag_names())
        self.assertIn("md_inline", self.app.chat_text.tag_names())
        self.assertIn("md_bold", self.app.chat_text.tag_names())

    def test_delivery_report_lists_files_without_answer(self):
        self.real_config()
        script = os.path.join(self.app.state.root, "delivery_demo.py")
        ops = [("write_file", {"path": script, "content": "print(1)\n"}),
               ("read_file", {"path": script})]
        self.app.input.insert("1.0", "演示完整流程")
        with patch.object(AgentLLMClient, "complete", self.scripted(ops)):
            self.app.send()
            self.wait_task()
        self.assertIn("状态：完成", self.app.report)
        self.assertIn("## 输出文件夹", self.app.report)
        self.assertIn("## 输出文件", self.app.report)
        self.assertIn("delivery_demo.py", self.app.report)
        self.assertNotIn("演示完整流程", self.app.report)
        self.assertIn("md_h2", self.app.delivery.tag_names())

    def test_mode_switch_has_no_transition_animation(self):
        import clawx.widgets as widget_module
        self.assertFalse(hasattr(widget_module, "FadeOverlay"))
        self.assertFalse(hasattr(self.app, "_animate_mode"))
        self.app.set_work_mode("chat")
        self.assertFalse(hasattr(self.app, "_mode_fade"))
        overlays = [child for child in self.app.main_area.winfo_children()
                    if child.winfo_class() == "Canvas"]
        self.assertEqual(overlays, [])
        self.app.set_chat_style("deep")
        self.assertFalse(hasattr(self.app, "_mode_fade"))
        self.root.update()
        overlays = [child for child in self.app.main_area.winfo_children()
                    if child.winfo_class() == "Canvas"]
        self.assertEqual(overlays, [])

    def test_instant_answer_offers_three_levels(self):
        from clawx.core import INSTANT_LABELS
        self.app.set_work_mode("chat")
        self.assertEqual(self.app.config.chat_style, "instant")
        self.assertEqual(self.app.thinking_slider.levels, ["Low", "Medium", "High"])
        self.assertEqual(self.app.thinking_slider.labels, INSTANT_LABELS)
        self.app.set_thinking_level("Medium")
        self.assertEqual(self.app.config.effective_level(), "Medium")
        self.assertEqual(self.app.thinking_menu_button.level, "Medium")

    def test_config_dialog_window_fits_footer_buttons(self):
        dialog = ConfigDialog(self.app)
        self.root.update()
        # 窗口高度必须足以容纳全部表单内容，保证底部按钮完全可见。
        height = min(880, dialog.winfo_screenheight() - 80)
        dialog.update_idletasks()
        self.assertGreaterEqual(height, dialog.winfo_reqheight())
        self.assertGreaterEqual(height, 600)
        def descend(parent):
            for child in parent.winfo_children():
                yield child
                yield from descend(child)
        texts = [w.cget("text") for w in descend(dialog) if isinstance(w, ttk.Button)]
        self.assertIn("保存并关闭", texts)
        self.assertIn("测试真实连接", texts)
        dialog.destroy()

    def test_config_dialog_adds_model_with_supported_levels(self):
        dialog = ConfigDialog(self.app)
        dialog.new()
        dialog.label_var.set("GPT-4o 迷你")
        dialog.fields["model"].set("gpt-4o-mini")
        dialog.level_vars["Max"].set(False)
        dialog.level_vars["Ultra"].set(False)
        self.assertTrue(dialog.save_entry())
        entries = self.app.models.all()
        labels = [item["label"] for item in entries]
        self.assertIn("GPT-4o 迷你", labels)
        saved = next(item for item in entries if item["label"] == "GPT-4o 迷你")
        self.assertEqual(saved["levels"], ["Low", "Medium", "High"])
        dialog.destroy()

    def test_model_context_menu_targets_row_and_replaces_delete_button(self):
        entries = self.app.models.all()
        secondary = {**entries[0], "id": uuid.uuid4().hex, "label": "备用模型", "model": "backup"}
        self.app.models.replace(entries + [secondary], entries[0]["id"])
        self.root.deiconify()
        dialog = ConfigDialog(self.app)
        dialog.deiconify()
        self.root.update()
        bounds = dialog.listbox.bbox(1)
        self.assertIsNotNone(bounds)
        event = SimpleNamespace(keysym="??", y=bounds[1] + 1, x_root=100, y_root=100)
        with patch.object(dialog.model_context_menu, "tk_popup") as popup:
            dialog.show_model_menu(event)
        popup.assert_called_once()
        self.assertEqual(dialog.selected, 1)
        with patch("clawx.ui.simpledialog.askstring", return_value="重命名备用"):
            dialog.model_context_menu.invoke("重命名")
        self.assertEqual(self.app.models.by_id(secondary["id"])["label"], "重命名备用")
        self.assertEqual(self.app.models.by_id(secondary["id"])["model"], "backup")
        self.assertEqual(self.app.models.active_id, entries[0]["id"])
        self.assertIn("重命名备用", self.app.model_combo.cget("values"))
        def descend(parent):
            for widget in parent.winfo_children():
                yield widget
                yield from descend(widget)
        self.assertFalse(any(isinstance(widget, ttk.Button) and widget.cget("text") == "删除"
                             for widget in descend(dialog)))
        dialog.model_context_menu.invoke("删除")
        self.assertIsNone(self.app.models.by_id(secondary["id"]))
        self.assertNotIn("重命名备用", self.app.model_combo.cget("values"))
        dialog.destroy()

    def test_model_rename_validation_cancel_and_active_model_refresh(self):
        entries = self.app.models.all()
        self.app.models.replace(entries + [{**entries[0], "id": uuid.uuid4().hex, "label": "已存在"}],
                                entries[0]["id"])
        dialog = ConfigDialog(self.app)
        snapshot = self.app.models.all()
        for name in (" ", "已存在", "x" * 61):
            with self.subTest(name=name), patch("clawx.ui.simpledialog.askstring", return_value=name), \
                    patch("clawx.ui.messagebox.showerror") as error:
                dialog.rename()
                error.assert_called_once()
                self.assertEqual(self.app.models.all(), snapshot)
        with patch("clawx.ui.simpledialog.askstring", return_value=None):
            dialog.rename()
        self.assertEqual(self.app.models.all(), snapshot)
        with patch("clawx.ui.simpledialog.askstring", return_value="当前模型新名字"):
            dialog.rename()
        self.assertEqual(self.app.models.active_id, entries[0]["id"])
        self.assertEqual(self.app.model_combo.get(), "当前模型新名字")
        with patch("clawx.ui.messagebox.showerror") as error:
            dialog.delete()
        error.assert_called_once()
        dialog.destroy()

    def test_config_dialog_save_applies_key_and_timeout(self):
        dialog = ConfigDialog(self.app)
        dialog.fields["model"].set("deepseek-chat")
        dialog.fields["api_key"].set("sk-test")
        dialog.fields["audit_model"].set("review-model")
        dialog.timeout.set("60")
        dialog.save_and_close()
        # 保存必须真正生效：密钥/模型/审查模型/超时全部写入应用配置。
        self.assertEqual(self.app.config.api_key, "sk-test")
        self.assertEqual(self.app.config.model, "deepseek-chat")
        self.assertEqual(self.app.config.audit_model, "review-model")
        self.assertEqual(self.app.config.timeout, 60)
        saved = json.loads((self.app.data_dir / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["timeout"], 60)
        self.assertNotIn("mock", saved)
        self.assertNotIn("api_key", saved)

    def test_saved_real_config_drives_real_chat_in_both_styles(self):
        dialog = ConfigDialog(self.app)
        dialog.fields["model"].set("deepseek-chat")
        dialog.fields["api_key"].set("sk-test")
        dialog.save_and_close()
        self.assertEqual(self.app.config.api_key, "sk-test")
        self.app.set_work_mode("chat")
        for style in ("instant", "deep"):
            with self.subTest(style=style):
                self.app.set_chat_style(style)
                self.app.input.insert("1.0", "你好")
                with patch.object(AgentLLMClient, "complete",
                                  return_value={"content": f"真实回复[{style}]", "tool_calls": []}) as call:
                    self.app.send()
                    self.wait_task()
                self.assertTrue(call.called)
                content = self.app.chat_text.get("1.0", "end")
                self.assertIn(f"真实回复[{style}]", content)
                self.assertNotIn("Mock", content)
                self.app.new_task()

    def test_startup_restores_key_and_keeps_real_mode(self):
        self.app.close()
        model_id = uuid.uuid4().hex
        data_dir = Path(self.temp.name) / ".clawx-data"
        (data_dir / "models.json").write_text(json.dumps({
            "active_id": model_id,
            "models": [{"id": model_id, "label": "DeepSeek", "model": "deepseek-chat",
                        "provider": "openai", "base_url": "https://api.deepseek.com/v1",
                        "api_key": "sk-restored", "reasoning_api": "portable",
                        "effort_ceiling": "high",
                        "levels": ["Low", "Medium", "High", "Max", "Ultra"]}],
        }, ensure_ascii=False), encoding="utf-8")
        (data_dir / "settings.json").write_text(json.dumps({
            "model": "deepseek-chat", "provider": "openai",
            "base_url": "https://api.deepseek.com/v1", "active_model_id": model_id,
            "work_mode": "work", "thinking_level": "Medium", "chat_style": "instant",
            "reasoning_api": "portable", "effort_ceiling": "high", "theme": "dark",
        }, ensure_ascii=False), encoding="utf-8")
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = ClawXApp(self.root, Path(self.temp.name))
        # 重启后从 models.json 恢复密钥，保持真实模式。
        self.assertEqual(self.app.config.api_key, "sk-restored")
        self.assertEqual(self.app.config.model, "deepseek-chat")


    def test_conversation_storage_and_sidebar_listing(self):
        self.real_config()
        self.app.set_work_mode("chat")
        self.app.set_chat_style("deep")
        self.app.input.insert("1.0", "存储对话测试")
        with patch.object(AgentLLMClient, "complete",
                          return_value={"content": "真实回复内容", "tool_calls": []}):
            self.app.send()
            self.wait_task()
        records = self.app.conversations.all(self.app.projects.active_id)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["title"], "存储对话测试")
        roles = [m["role"] for m in record["messages"]]
        self.assertEqual(roles, ["user", "assistant"])
        self.assertIn("存储对话测试", record["messages"][0]["content"])
        self.assertIn("真实回复内容", record["messages"][1]["content"])
        self.assertEqual(record["messages"][1]["mode"], "chat")
        self.assertEqual(self.app.conv_listbox.size(), 1)
        data = json.loads((self.app.data_dir / "conversations.json").read_text(encoding="utf-8"))
        self.assertEqual(len(data["conversations"]), 1)

    def test_load_and_delete_conversation(self):
        self.real_config()
        self.app.set_work_mode("chat")
        self.app.set_chat_style("deep")
        self.app.input.insert("1.0", "历史对话测试")
        with patch.object(AgentLLMClient, "complete",
                          return_value={"content": "历史回复", "tool_calls": []}):
            self.app.send()
            self.wait_task()
        record_id = self.app.conversations.all(self.app.projects.active_id)[0]["id"]
        self.app.new_task()
        self.assertEqual(self.app.chat_text.get("1.0", "end-1c").strip(), "")
        # 侧栏列表只有当前项目的记录
        self.assertEqual(self.app.conv_listbox.size(), 1)
        self.app.load_conversation(record_id)
        content = self.app.chat_text.get("1.0", "end")
        self.assertIn("历史对话测试", content)
        self.assertIn("历史回复", content)
        self.assertEqual(self.app.agent.histories["chat"][-1]["content"], "历史回复")
        self.app.conv_listbox.selection_set(0)
        self.app._conversation_deleted()
        self.assertEqual(self.app.conversations.all(self.app.projects.active_id), [])
        self.assertEqual(self.app.conv_listbox.size(), 0)


    def test_branch_dialog_continues_from_prefix_without_later_context(self):
        self.real_config()
        self.app.set_work_mode("chat")
        for question, answer in (("第一轮", "第一答"), ("后续不能带入", "后续回答")):
            self.app.input.insert("1.0", question)
            with patch.object(AgentLLMClient, "complete", self.scripted([], final=answer)):
                self.app.send()
                self.wait_task()
        source_id = self.app.session_id
        source = self.app.conversations.by_id(source_id)
        dialog = self.app._open_branch_dialog()
        self.assertIsInstance(dialog, BranchDialog)
        dialog.message_list.selection_clear(0, "end")
        dialog.message_list.selection_set(1)
        dialog.preview_message()
        self.assertIn("第一答", dialog.preview.get("1.0", "end"))
        dialog.title_var.set("试试另一个方向")
        dialog.create()
        branch_id = self.app.session_id
        branch = self.app.conversations.by_id(branch_id)
        self.assertNotEqual(branch_id, source_id)
        self.assertEqual(branch["parent_id"], source_id)
        self.assertEqual(branch["branch_at"], 1)
        self.assertEqual(branch["messages"], source["messages"][:2])
        self.assertNotIn("后续不能带入", self.app.chat_text.get("1.0", "end"))
        captured = []
        def complete(_client, messages, **_kwargs):
            captured.extend(messages)
            return {"content": "分支独立回答", "tool_calls": []}
        self.app.input.insert("1.0", "新的方向")
        with patch.object(AgentLLMClient, "complete", complete):
            self.app.send()
            self.wait_task()
        self.assertIn("第一答", json.dumps(captured, ensure_ascii=False))
        self.assertNotIn("后续不能带入", json.dumps(captured, ensure_ascii=False))
        continued = self.app.conversations.by_id(branch_id)
        self.assertEqual(continued["title"], "试试另一个方向")
        self.assertEqual(continued["parent_id"], source_id)
        self.assertEqual(self.app.conversations.by_id(source_id)["messages"], source["messages"])
        self.app.conversations.delete(source_id)
        self.app.load_conversation(branch_id)
        self.assertIn("分支独立回答", self.app.chat_text.get("1.0", "end"))

    def test_development_branch_uses_public_reply_and_blocks_cross_project_or_busy(self):
        from clawx.core import PolicyError
        self.real_config()
        self.app.input.insert("1.0", "开发问题")
        with patch.object(AgentLLMClient, "complete", self.scripted([], final="实现建议正文")):
            self.app.send()
            self.wait_task()
        source_id = self.app.session_id
        source = self.app.conversations.by_id(source_id)
        self.assertEqual(source["messages"][-1]["content"], "实现建议正文")
        self.app.ui_busy = True
        with self.assertRaises(PolicyError):
            self.app.create_branch(source_id, 1)
        self.app.ui_busy = False
        other = {**source, "id": uuid.uuid4().hex, "project_id": "other-project"}
        self.app.conversations.upsert(other)
        with self.assertRaises(PolicyError):
            self.app.create_branch(other["id"], 1)
        self.app.set_work_mode("chat")
        self.app.create_branch(source_id, 1)
        self.assertEqual(self.app.state.work_mode, "work")
        self.assertEqual(self.app.agent.history[-1]["content"], "实现建议正文")
        self.assertNotIn("ClawX 交付文件", self.app.chat_text.get("1.0", "end"))

    def test_quick_composer_is_minimal_reusable_and_keeps_draft_on_hide(self):
        self.app.show_quick_composer()
        self.root.update()
        window = self.app.quick_window
        self.assertEqual(self.root.state(), "withdrawn")
        widgets = window.panel.winfo_children()
        self.assertEqual(sum(isinstance(widget, tk.Text) for widget in widgets), 1)
        self.assertEqual([widget.cget("text") for widget in widgets if isinstance(widget, tk.Button)],
                         ["进入完整页面", "发送"])
        self.assertEqual(len(widgets), 3)
        window.input.insert("1.0", "保留草稿")
        window.hide()
        self.assertEqual(window.state(), "withdrawn")
        self.app.show_quick_composer()
        self.assertIs(self.app.quick_window, window)
        self.assertEqual(window.input.get("1.0", "end-1c"), "保留草稿")

    def test_quick_send_uses_chat_without_sending_existing_full_page_draft(self):
        self.real_config()
        self.app.input.insert("1.0", "未发送的完整页草稿")
        self.app.show_quick_composer()
        window = self.app.quick_window
        window.input.insert("1.0", "快速问题")
        captured, options = [], []
        def complete(_client, messages, **kwargs):
            captured.extend(messages)
            options.append(kwargs)
            return {"content": "快速回答", "tool_calls": []}
        with patch.object(AgentLLMClient, "complete", complete):
            window.submit()
            self.wait_task()
        self.assertEqual(self.app.state.work_mode, "chat")
        self.assertFalse(options[0]["tools"])
        self.assertIn("快速问题", json.dumps(captured, ensure_ascii=False))
        self.assertNotIn("未发送的完整页草稿", json.dumps(captured, ensure_ascii=False))
        self.assertEqual(self.app.input.get("1.0", "end-1c"), "未发送的完整页草稿")
        self.assertIn("快速回答", self.app.chat_text.get("1.0", "end"))
        self.assertEqual(window.state(), "withdrawn")
        self.assertNotEqual(self.root.state(), "withdrawn")

    def test_quick_full_page_transfers_draft_without_sending_and_busy_disables_send(self):
        self.app.show_quick_composer()
        window = self.app.quick_window
        window.input.insert("1.0", "带入完整界面")
        with patch.object(self.app, "send") as send:
            window.open_full()
        send.assert_not_called()
        self.assertEqual(self.app.input.get("1.0", "end-1c"), "带入完整界面")
        self.app.ui_busy = True
        self.app.show_quick_composer()
        self.assertEqual(window.send_button.cget("state"), "disabled")
        window.input.insert("1.0", "留给下一次")
        with patch.object(self.app, "send") as send:
            window.submit()
        send.assert_not_called()
        self.assertEqual(window.input.get("1.0", "end-1c"), "留给下一次")
        self.app.ui_busy = False

    def test_wake_event_has_priority_over_long_token_queue_and_setting_persists(self):
        self.app.events.put(("token", {"text": "长回答" * 200}))
        self.app.quick_events.put(("quick_wake", None))
        self.app.pump()
        self.assertIsNotNone(self.app.quick_window)
        self.assertTrue(self.app._token_pending)
        self.app.quick_window.hide()
        self.app.agent.cancel.set()
        self.app.pump()
        self.app._stream_finalize()
        dialog = ConfigDialog(self.app)
        dialog.fields["model"].set("test-model")
        dialog.quick_hotkey.set("Ctrl+Shift+Space")
        dialog.save_and_close()
        self.assertEqual(self.app.config.quick_hotkey, "Ctrl+Shift+Space")
        self.assertEqual(self.app.wake_hotkey.shortcut, "Ctrl+Shift+Space")
        stored = json.loads((self.app.data_dir / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["quick_hotkey"], "Ctrl+Shift+Space")

    def test_plan_review_window_pops_before_first_tools(self):
        self.real_config()
        script = os.path.join(self.app.state.root, "plan_gate.py")
        ops = [("write_file", {"path": script, "content": "print(1)\n"})]
        self.app.input.insert("1.0", "写个文件")
        with patch.object(AgentLLMClient, "complete", self.scripted(ops)):
            self.app.send()
            deadline = time.monotonic() + 5
            while not self.app.review and time.monotonic() < deadline:
                self.root.update()
                time.sleep(0.01)
            self.assertIsNotNone(self.app.review)
            self.assertEqual(self.app.review.request.kind, "plan_review")
            self.app.review.finish(True, self.app.review.request.editable)
            self.wait_task()
        self.assertIn("已制定计划，等待审批…", self.app.chat_text.get("1.0", "end"))
        self.assertTrue(Path(script).exists())

    def test_tool_progress_lines_in_chat(self):
        self.real_config()
        script = os.path.join(self.app.state.root, "progress_demo.py")
        ops = [("write_file", {"path": script, "content": "print(1)\n"}),
               ("read_file", {"path": script})]
        self.app.input.insert("1.0", "读写文件")
        with patch.object(AgentLLMClient, "complete", self.scripted(ops)):
            self.app.send()
            self.wait_task()
        content = self.app.chat_text.get("1.0", "end")
        self.assertEqual(content.count("【工具调用】write_file"), 1)
        self.assertEqual(content.count("【工具调用】read_file"), 1)
        self.assertEqual(content.count("执行结果 ›"), 2)
        self.assertNotIn("progress_demo.py", content)
        self.assertNotIn('"content":', content)
        self.assertNotIn("运行策略", content)
        self.assertIn("执行了 ", content)
        self.assertNotIn("执行中 ·", content)
        self.assertIsNone(self.app._execution_started)

    def test_compact_details_are_clickable_and_preserve_full_payload(self):
        body = "完整内容" * 900
        operation = {"tool": "write_file", "arguments": {"path": "example.py", "content": body}}
        result = {"path": "example.py", "content": body}
        self.app.chat("工具", json.dumps(operation, ensure_ascii=False))
        self.app.chat("结果", json.dumps(result, ensure_ascii=False))
        content = self.app.chat_text.get("1.0", "end")
        self.assertNotIn(body, content)
        self.assertNotIn("查看参数", content)
        for role, expected in (("工具", operation), ("结果", result)):
            index = next(i for i, entry in enumerate(self.app.chat_log, 1) if entry[0] == role)
            tag = f"request_{index}"
            callback = self.root.tk.call(self.app.chat_text._w, "tag", "bind", tag, "<Button-1>")
            self.assertTrue(callback)
            # Invoke the same Tcl-bound callback Tk runs on a clicked tagged row.
            command = callback.split("[")[1].split()[0]
            with patch.object(self.app, "_show_text") as show:
                self.root.tk.call(command, *(["0"] * 19))
            self.assertEqual(json.loads(show.call_args.args[1]), expected)

    def test_elapsed_header_uses_real_duration_and_is_separate_from_chat(self):
        self.app.chat("用户", "查看项目")
        self.app.chat("执行时间", "执行中 · <1s")
        self.app.chat("工具", '{"tool":"list_files","arguments":{"path":"."}}')
        self.app._set_execution_time(300.8, finished=True)
        self.app.chat("错误", "PolicyError: API HTTP 402，请检查地址、模型、额度与凭据。")
        content = self.app.chat_text.get("1.0", "end")
        self.assertEqual(content.count("执行了 5min"), 1)
        self.assertLess(content.index("执行了 5min"), content.index("【工具调用】"))
        self.assertTrue(self.app.chat_text.tag_ranges("execution_rule"))
        self.assertIn("PolicyError: API HTTP 402", content)
        self.assertEqual(self.app._format_elapsed(0.4), "<1s")
        self.assertEqual(self.app._format_elapsed(61.9), "1min 1s")
        self.assertEqual(self.app._format_elapsed(3601), "1h 1s")
        self.app.set_theme("light")
        self.assertIn("执行了 5min", self.app.chat_text.get("1.0", "end"))

    def test_all_running_strategy_messages_are_hidden(self):
        for mode in ("work", "chat"):
            self.app.set_work_mode(mode)
            self.app.chat("思考", "不应显示的运行策略")
            self.app.events.put(("thinking", {"mode": mode, "text": "不应显示的运行策略"}))
            self.app.pump()
        self.assertNotIn("运行策略", self.app.chat_text.get("1.0", "end"))
        self.assertFalse(any(role == "思考" for role, _ in self.app.chat_log))

    def test_streaming_token_rendering(self):
        self.real_config()
        self.app.set_work_mode("chat")
        self.app.set_chat_style("deep")

        def complete(_client, _messages, stream=False, on_token=None, **_kwargs):
            if on_token:
                for piece in ("这是", "逐字", "输出"):
                    on_token(piece)
            return {"content": "这是逐字输出", "tool_calls": []}

        self.app.input.insert("1.0", "你好")
        with patch.object(AgentLLMClient, "complete", complete):
            self.app.send()
            self.wait_task()
        content = self.app.chat_text.get("1.0", "end")
        self.assertEqual(content.count("这是逐字输出"), 1)
        self.assertIn(("ClawX", "这是逐字输出"), self.app.chat_log)

    def test_development_displays_partial_reply_before_model_finishes(self):
        self.real_config()
        released = threading.Event()
        final = "首段可见，后续完成。"

        def complete(_client, _messages, stream=False, on_token=None, **_kwargs):
            self.assertTrue(stream)
            on_token("首段可见")
            released.wait(timeout=5)
            on_token("，后续完成。")
            return {"content": final, "tool_calls": []}

        self.app.input.insert("1.0", "解释当前项目")
        with patch.object(AgentLLMClient, "complete", complete):
            self.app.send()
            try:
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    self.root.update()
                    if "首段可见" in self.app.chat_text.get("1.0", "end"):
                        break
                    time.sleep(0.01)
                self.assertIn("首段可见", self.app.chat_text.get("1.0", "end"))
                self.assertNotIn("后续完成", self.app.chat_text.get("1.0", "end"))
                self.assertTrue(self.app.agent.running)
                self.assertTrue(self.app.ui_busy)
            finally:
                released.set()
            self.wait_task()
        self.assertEqual(self.app.chat_text.get("1.0", "end").count(final), 1)
        self.assertEqual(self.app.chat_log.count(("ClawX", final)), 1)

    def test_large_delta_reveals_in_frames_before_following_error(self):
        self.app.welcome.place_forget()
        self.app.agent.cancel.clear()
        text = "逐步显示的文字" * 30
        self.app.events.put(("token", {"text": text}))
        self.app.events.put(("stream_end", {"text": text}))
        self.app.events.put(("chat", ("错误", "FOLLOWING_ERROR")))
        self.app.pump()
        content = self.app.chat_text.get("1.0", "end")
        self.assertTrue(self.app._stream_active)
        self.assertTrue(self.app._token_pending)
        self.assertNotIn(text, content)
        self.assertNotIn("FOLLOWING_ERROR", content)
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and (self.app._stream_active or not self.app.events.empty()):
            self.root.update()
            time.sleep(0.01)
        content = self.app.chat_text.get("1.0", "end")
        self.assertIn(text, content)
        self.assertGreater(content.index("FOLLOWING_ERROR"), content.index(text))
        self.assertIn(("ClawX", text), self.app.chat_log)

    def test_partial_error_is_finalized_before_next_message(self):
        self.real_config()
        self.app.set_work_mode("chat")

        def fail(_client, _messages, on_token=None, **_kwargs):
            on_token("保留已生成部分")
            raise RuntimeError("断线")

        self.app.input.insert("1.0", "第一次")
        with patch.object(AgentLLMClient, "complete", fail):
            self.app.send()
            self.wait_task()
        self.assertFalse(self.app._stream_active)
        self.assertIn(("ClawX", "保留已生成部分"), self.app.chat_log)
        self.app.input.insert("1.0", "第二次")
        with patch.object(AgentLLMClient, "complete", self.scripted([], final="新的回复")):
            self.app.send()
            self.wait_task()
        self.assertIn(("ClawX", "新的回复"), self.app.chat_log)
        self.assertEqual(self.app.chat_text.get("1.0", "end").count("保留已生成部分"), 1)

    def test_stop_preserves_partial_reply_and_unlocks_input(self):
        from clawx.core import Cancelled
        self.real_config()
        self.app.set_work_mode("chat")

        def complete(client, _messages, on_token=None, **_kwargs):
            on_token("停止前的文字")
            client.cancel.wait(timeout=5)
            raise Cancelled("已停止生成")

        self.app.input.insert("1.0", "长回答")
        with patch.object(AgentLLMClient, "complete", complete):
            self.app.send()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                self.root.update()
                if "停止前的文字" in self.app.chat_text.get("1.0", "end"):
                    break
                time.sleep(0.01)
            self.assertIn("停止前的文字", self.app.chat_text.get("1.0", "end"))
            self.app.stop()
            self.wait_task()
        self.assertFalse(self.app._stream_active)
        self.assertFalse(self.app._token_pending)
        self.assertFalse(self.app.ui_busy)
        self.assertIn(("ClawX", "停止前的文字"), self.app.chat_log)
        self.assertIn("已停止生成", self.app.chat_text.get("1.0", "end"))

    def test_chat_reply_claiming_files_gets_warning(self):
        self.real_config()
        self.app.set_work_mode("chat")
        self.app.set_chat_style("deep")
        self.app.input.insert("1.0", "生成报告")
        with patch.object(AgentLLMClient, "complete",
                          return_value={"content": "我已生成 report.md 文件", "tool_calls": []}):
            self.app.send()
            self.wait_task()
        content = self.app.chat_text.get("1.0", "end")
        self.assertIn("标准聊天模式无法创建或修改任何文件", content)


if __name__ == "__main__":
    unittest.main()
