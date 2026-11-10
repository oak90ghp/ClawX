"""Tk dashboard. All Tk access stays on the main thread."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
import json
import os
import queue
import threading
import time
import tkinter as tk
import urllib.parse
import uuid
from tkinter import filedialog, messagebox, simpledialog, ttk

from .agent import AgentController
from .core import (ApprovalRequest, ConversationManager, PolicyError, PolicyState,
                   ProjectManager, SkillManager, ModelRegistry, VALID_THINKING_LEVELS,
                   VALID_WORK_MODES, THINKING_LEVELS, INSTANT_LEVELS, nearest_level)
from .llm import (AgentLLMClient, LLMConfig, REASONING_APIS,
                  EFFORT_CEILINGS, default_config)
from .hotkeys import GlobalHotkey, QUICK_HOTKEYS
from .widgets import (
    theme, UI_FONT, CODE_FONT, TIERS, TIER_COLORS,
    WORK_MODE_LABELS, CHAT_STYLE_LABELS,
    text_box, set_text, render_markdown, RoundedPanel,
)
from .layout import DashboardLayout


class ReviewDialog(tk.Toplevel):
    def __init__(self, app, request: ApprovalRequest):
        super().__init__(app.root)
        self.app, self.request = app, request
        self.title("ClawX · " + request.title)
        self.configure(bg=theme.bg)
        self.geometry("950x760")
        self.minsize(680, 480)
        self.transient(app.root)
        self.protocol("WM_DELETE_WINDOW", lambda: self.finish(False))
        ttk.Label(self, text=request.title, style="Title.TLabel").pack(anchor="w", padx=16, pady=(12, 4))
        ttk.Label(self, text="请核对操作范围和变更。编辑下方 JSON 后须点击“修改后执行”。").pack(anchor="w", padx=16, pady=(0, 8))
        panes = ttk.Panedwindow(self, orient="vertical")
        panes.pack(fill="both", expand=True, padx=16)
        preview = ttk.LabelFrame(panes, text="操作 / Diff / 原始计划白名单", padding=4)
        editor = ttk.LabelFrame(panes, text="可编辑的操作或完整计划 JSON", padding=4)
        panes.add(preview, weight=1)
        panes.add(editor, weight=2)
        set_text(text_box(preview), request.text)
        self.editor = text_box(editor, editable=True, wrap="none")
        set_text(self.editor, request.editable)
        buttons = ttk.Frame(self, padding=14)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="拒绝", style="Danger.TButton", command=lambda: self.finish(False)).pack(side="left")
        ttk.Button(buttons, text="修改后执行", command=self.modified).pack(side="right", padx=(8, 0))
        ttk.Button(buttons, text="允许执行" if request.kind in {"operation", "plan_review"} else "批准并锁定原计划",
                   style="Primary.TButton", command=lambda: self.finish(True, request.editable)).pack(side="right")
        self.grab_set()
        self.focus_after = self.after(50, self.focus_force)

    def modified(self):
        value = self.editor.get("1.0", "end-1c")
        try:
            json.loads(value)
        except ValueError as exc:
            messagebox.showerror("JSON 格式错误", str(exc), parent=self)
            return
        self.finish(True, value)

    def finish(self, allowed, value=""):
        self.after_cancel(self.focus_after)
        self.request.answer = (allowed, value)
        self.request.done.set()
        self.grab_release()
        if self.app.review is self:
            self.app.review = None
        self.destroy()


class SkillDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.app, self.manager = app, app.skills
        self.items = self.manager.all()
        self.selected = None
        self.title("ClawX · Skill 管理中心")
        self.geometry("900x660")
        self.configure(bg=theme.bg)
        self.transient(app.root)
        ttk.Label(self, text="Skill 管理中心", style="Title.TLabel").pack(anchor="w", padx=16, pady=12)
        ttk.Label(self, text="保存后实时生效。allowed_tools 为硬限制：多个启用 Skill 取交集；留空不额外限制。"
                  ).pack(anchor="w", padx=16, pady=(0, 10))
        panes = ttk.Panedwindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=16)
        left, right = ttk.Frame(panes), ttk.Frame(panes, padding=(12, 0, 0, 0))
        panes.add(left, weight=1)
        panes.add(right, weight=3)
        self.listbox = tk.Listbox(left, bg=theme.field, fg=theme.fg, selectbackground=theme.select, relief="flat",
                                  highlightthickness=0, font=(UI_FONT, 10), exportselection=False)
        self.listbox.pack(fill="both", expand=True)
        self.listbox.bind("<<ListboxSelect>>", self.choose)
        row = ttk.Frame(left)
        row.pack(fill="x", pady=8)
        ttk.Button(row, text="新增", command=self.new).pack(side="left")
        ttk.Button(row, text="删除", command=self.delete).pack(side="right")
        self.name, self.description, self.allowed = tk.StringVar(), tk.StringVar(), tk.StringVar()
        self.enabled = tk.BooleanVar(value=True)
        for label, variable in (("名称", self.name), ("触发条件 / 描述", self.description),
                                ("allowed_tools（英文逗号分隔）", self.allowed)):
            ttk.Label(right, text=label).pack(anchor="w", pady=(6, 4))
            ttk.Entry(right, textvariable=variable).pack(fill="x")
        ttk.Checkbutton(right, text="启用此 Skill", variable=self.enabled).pack(anchor="w", pady=10)
        ttk.Label(right, text="instruction · 执行指南 / Prompt").pack(anchor="w")
        self.instruction = text_box(right, editable=True, height=10)
        footer = ttk.Frame(self, padding=16)
        footer.pack(fill="x")
        ttk.Button(footer, text="导入 JSON", command=self.import_json).pack(side="left")
        ttk.Button(footer, text="保存当前 Skill", style="Primary.TButton", command=self.save).pack(side="right")
        self.refresh()

    def refresh(self):
        self.listbox.delete(0, "end")
        for item in self.items:
            self.listbox.insert("end", ("● " if item["enabled"] else "○ ") + item["name"])

    def choose(self, _event=None):
        selected = self.listbox.curselection()
        if not selected:
            return
        self.selected = selected[0]
        item = self.items[self.selected]
        self.name.set(item["name"])
        self.description.set(item["description"])
        self.allowed.set(", ".join(item["allowed_tools"]))
        self.enabled.set(item["enabled"])
        set_text(self.instruction, item["instruction"])

    def new(self):
        self.selected = None
        self.listbox.selection_clear(0, "end")
        self.name.set("")
        self.description.set("")
        self.allowed.set("")
        self.enabled.set(True)
        set_text(self.instruction, "")

    def persist(self, candidate):
        self.manager.replace(candidate)
        self.items = self.manager.all()
        self.refresh()
        self.app.chat("Skill", "Skill 配置已保存，下一次工具校验和模型请求实时生效。")

    def save(self):
        item = {"name": self.name.get(), "description": self.description.get(),
                "instruction": self.instruction.get("1.0", "end-1c"), "enabled": self.enabled.get(),
                "allowed_tools": [s.strip() for s in self.allowed.get().split(",") if s.strip()]}
        candidate = list(self.items)
        if self.selected is None:
            candidate.append(item)
        else:
            candidate[self.selected] = item
        try:
            self.persist(candidate)
            self.new()
        except (ValueError, OSError) as exc:
            messagebox.showerror("保存失败", str(exc), parent=self)

    def delete(self):
        if self.selected is None:
            return
        try:
            self.persist([x for i, x in enumerate(self.items) if i != self.selected])
            self.new()
        except OSError as exc:
            messagebox.showerror("删除失败", str(exc), parent=self)

    def import_json(self):
        path = filedialog.askopenfilename(parent=self, filetypes=[("Skill JSON", "*.json")])
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data = [data]
            if not isinstance(data, list):
                raise ValueError("JSON 必须是 Skill 对象或数组")
            self.persist(self.items + data)
            self.new()
        except (ValueError, OSError) as exc:
            messagebox.showerror("导入失败", str(exc), parent=self)


class ProjectDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.app, self.manager = app, app.projects
        self.items = self.manager.all()
        self.selected = None
        self.title("ClawX · 项目设置")
        self.geometry("980x720")
        self.minsize(780, 560)
        self.configure(bg=theme.bg)
        self.transient(app.root)
        ttk.Label(self, text="项目设置", style="Title.TLabel").pack(anchor="w", padx=18, pady=(14, 5))
        ttk.Label(self, text="为每个项目配置专属 Prompt、主工作目录和额外允许目录。路径会进入安全沙箱白名单。"
                  ).pack(anchor="w", padx=18, pady=(0, 12))
        panes = ttk.Panedwindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=18)
        left, right = ttk.Frame(panes), ttk.Frame(panes, padding=(14, 0, 0, 0))
        panes.add(left, weight=1)
        panes.add(right, weight=3)
        self.listbox = tk.Listbox(left, bg=theme.field, fg=theme.fg, selectbackground=theme.select, relief="flat",
                                  highlightthickness=0, font=(UI_FONT, 10), exportselection=False)
        self.listbox.pack(fill="both", expand=True)
        self.listbox.bind("<<ListboxSelect>>", self.choose)
        actions = ttk.Frame(left)
        actions.pack(fill="x", pady=8)
        ttk.Button(actions, text="新增", command=self.new).pack(side="left")
        ttk.Button(actions, text="删除", style="Danger.TButton", command=self.delete).pack(side="right")
        self.name, self.root_path = tk.StringVar(), tk.StringVar()
        ttk.Label(right, text="项目名称").pack(anchor="w", pady=(2, 4))
        ttk.Entry(right, textvariable=self.name).pack(fill="x")
        ttk.Label(right, text="主工作目录 · 相对路径以此为基准").pack(anchor="w", pady=(10, 4))
        root_row = ttk.Frame(right)
        root_row.pack(fill="x")
        ttk.Entry(root_row, textvariable=self.root_path).pack(side="left", fill="x", expand=True)
        ttk.Button(root_row, text="选择…", command=self.choose_root).pack(side="left", padx=(7, 0))
        ttk.Label(right, text="额外允许访问的文件夹 · 绝对路径必须位于这些目录或主目录中").pack(anchor="w", pady=(10, 4))
        folder_host = ttk.Frame(right)
        folder_host.pack(fill="both", expand=False)
        self.folders = tk.Listbox(folder_host, height=5, bg=theme.field, fg=theme.fg, selectbackground=theme.select,
                                  relief="flat", highlightthickness=0, exportselection=False)
        self.folders.pack(side="left", fill="both", expand=True)
        folder_actions = ttk.Frame(folder_host)
        folder_actions.pack(side="left", fill="y", padx=(7, 0))
        ttk.Button(folder_actions, text="添加…", command=self.add_folder).pack(fill="x")
        ttk.Button(folder_actions, text="移除", command=self.remove_folder).pack(fill="x", pady=6)
        ttk.Label(right, text="项目 Prompt · 将注入聊天与开发模式，不能覆盖安全策略").pack(anchor="w", pady=(12, 4))
        self.prompt = text_box(right, editable=True, height=10)
        footer = ttk.Frame(self, padding=18)
        footer.pack(fill="x")
        ttk.Button(footer, text="取消", command=self.destroy).pack(side="left")
        ttk.Button(footer, text="保存并切换到此项目", style="Primary.TButton",
                   command=self.save_and_activate).pack(side="right")
        self.refresh()
        active = next((i for i, item in enumerate(self.items) if item["id"] == self.manager.active_id), 0)
        self.listbox.selection_set(active)
        self.choose()

    def refresh(self):
        self.listbox.delete(0, "end")
        for item in self.items:
            marker = "● " if item["id"] == self.manager.active_id else "○ "
            self.listbox.insert("end", marker + item["name"])

    def _form_item(self, project_id=None):
        return {"id": project_id or uuid.uuid4().hex, "name": self.name.get(),
                "root": self.root_path.get(), "prompt": self.prompt.get("1.0", "end-1c"),
                "allowed_folders": list(self.folders.get(0, "end"))}

    def _store_form(self):
        if self.selected is not None:
            self.items[self.selected] = self._form_item(self.items[self.selected]["id"])

    def choose(self, _event=None):
        selected = self.listbox.curselection()
        if not selected:
            return
        index = selected[0]
        if self.selected is not None and self.selected != index:
            self._store_form()
        self.selected = index
        item = self.items[index]
        self.name.set(item["name"])
        self.root_path.set(item["root"])
        self.folders.delete(0, "end")
        for folder in item["allowed_folders"]:
            self.folders.insert("end", folder)
        set_text(self.prompt, item["prompt"])

    def new(self):
        self._store_form()
        item = {"id": uuid.uuid4().hex, "name": "新项目", "root": self.app.state.root,
                "prompt": "", "allowed_folders": []}
        self.items.append(item)
        self.refresh()
        self.listbox.selection_clear(0, "end")
        self.listbox.selection_set(len(self.items)-1)
        self.selected = None
        self.choose()

    def delete(self):
        if self.selected is None:
            return
        if len(self.items) == 1:
            messagebox.showerror("无法删除", "至少需要保留一个项目。", parent=self)
            return
        self.items.pop(self.selected)
        self.selected = None
        self.refresh()
        self.listbox.selection_set(0)
        self.choose()

    def choose_root(self):
        path = filedialog.askdirectory(parent=self, initialdir=self.root_path.get() or self.app.state.root,
                                       mustexist=True)
        if path:
            self.root_path.set(os.path.realpath(path))

    def add_folder(self):
        path = filedialog.askdirectory(parent=self, initialdir=self.root_path.get() or self.app.state.root,
                                       mustexist=True)
        if path:
            canonical = os.path.realpath(path)
            existing = {os.path.normcase(value) for value in self.folders.get(0, "end")}
            if os.path.normcase(canonical) not in existing:
                self.folders.insert("end", canonical)

    def remove_folder(self):
        for index in reversed(self.folders.curselection()):
            self.folders.delete(index)

    def save_and_activate(self):
        if self.selected is None:
            return
        self._store_form()
        selected_id = self.items[self.selected]["id"]
        try:
            self.manager.replace(self.items, selected_id)
            self.app.activate_project(selected_id)
        except (ValueError, OSError, PolicyError) as exc:
            messagebox.showerror("项目保存失败", str(exc), parent=self)
            return
        self.destroy()


class ConfigDialog(tk.Toplevel):
    """模型列表管理：每个模型可配置协议、地址、密钥、思考接口与支持的思考等级。

    API Key 保存在本地 .clawx-data/models.json（见 SECURITY.md），不会被上传。
    """

    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.manager = app.models
        self.items = self.manager.all()
        self.selected = next((i for i, item in enumerate(self.items)
                              if item["id"] == self.manager.active_id), 0)
        self.title("ClawX · 模型列表与连接")
        width = min(1080, self.winfo_screenwidth() - 60)
        height = min(880, self.winfo_screenheight() - 80)
        self.geometry(f"{width}x{height}")
        self.minsize(860, 640)
        self.configure(bg=theme.bg)
        self.transient(app.root)

        top = ttk.Frame(self, padding=(18, 12))
        top.pack(fill="x")
        ttk.Label(top, text="独立审查模型").pack(side="left")
        self.fields = {}
        self.fields["audit_model"] = tk.StringVar(value=app.config.audit_model)
        ttk.Entry(top, textvariable=self.fields["audit_model"], width=18).pack(side="left", padx=(6, 16))
        ttk.Label(top, text="等待时限（25–600 秒）").pack(side="left")
        self.timeout = tk.StringVar(value=str(app.config.timeout))
        ttk.Entry(top, textvariable=self.timeout, width=8).pack(side="left", padx=(6, 16))
        ttk.Label(top, text="外观").pack(side="left")
        self.theme_var = tk.StringVar(value=app.config.theme)
        ttk.Combobox(top, values=["dark", "light", "system"], textvariable=self.theme_var,
                     state="readonly", width=9).pack(side="left", padx=(6, 0))
        wake_row = ttk.Frame(self, padding=(18, 0))
        wake_row.pack(fill="x")
        ttk.Label(wake_row, text="快速唤醒快捷键").pack(side="left")
        self.quick_hotkey = tk.StringVar(value=app.config.quick_hotkey)
        ttk.Combobox(wake_row, textvariable=self.quick_hotkey, values=QUICK_HOTKEYS,
                     state="readonly", width=22).pack(side="left", padx=8)
        ttk.Label(wake_row, text="Windows 全局生效 · Alt+Alt 为连续轻按两次 Alt",
                  foreground=theme.muted).pack(side="left")

        panes = ttk.Panedwindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=18, pady=(10, 0))
        left, right = ttk.Frame(panes), ttk.Frame(panes, padding=(14, 0, 0, 0))
        panes.add(left, weight=1)
        panes.add(right, weight=3)
        ttk.Label(left, text="模型列表（● 当前 · 右键管理）").pack(anchor="w")
        self.listbox = tk.Listbox(left, bg=theme.field, fg=theme.fg, selectbackground=theme.select,
                                  relief="flat", highlightthickness=0, font=(UI_FONT, 10), exportselection=False)
        self.listbox.pack(fill="both", expand=True, pady=(6, 0))
        self.listbox.bind("<<ListboxSelect>>", self.choose)
        self.listbox.bind("<Button-3>", self.show_model_menu)
        self.listbox.bind("<Shift-F10>", self.show_model_menu)
        self.model_context_menu = tk.Menu(self, tearoff=False, bg=theme.field, fg=theme.fg,
                                          activebackground=theme.select, activeforeground=theme.fg)
        self.model_context_menu.add_command(label="重命名", command=self.rename)
        self.model_context_menu.add_command(label="删除", command=self.delete)
        actions = ttk.Frame(left)
        actions.pack(fill="x", pady=8)
        ttk.Button(actions, text="新增", command=self.new).pack(side="left")

        self.label_var = tk.StringVar()
        self.fields["model"] = tk.StringVar()
        self.fields["provider"] = tk.StringVar(value="openai")
        self.fields["base_url"] = tk.StringVar(value="https://api.openai.com/v1")
        self.fields["api_key"] = tk.StringVar()
        self.reasoning_api = tk.StringVar(value="portable")
        self.effort_ceiling = tk.StringVar(value="high")
        self.level_vars = {level: tk.BooleanVar(value=True) for level in THINKING_LEVELS}
        for label_text, key in (("名称（显示用）", self.label_var), ("模型 ID", self.fields["model"])):
            ttk.Label(right, text=label_text).pack(anchor="w", pady=(2, 4))
            ttk.Entry(right, textvariable=key).pack(fill="x")
        ttk.Label(right, text="协议：openai / anthropic").pack(anchor="w", pady=(8, 4))
        ttk.Combobox(right, values=["openai", "anthropic"], textvariable=self.fields["provider"],
                     state="readonly").pack(fill="x")
        ttk.Label(right, text="Base URL（含 /v1）").pack(anchor="w", pady=(8, 4))
        ttk.Entry(right, textvariable=self.fields["base_url"]).pack(fill="x")
        ttk.Label(right, text="API Key（保存在本地 models.json，请勿分享该文件夹）").pack(anchor="w", pady=(8, 4))
        ttk.Entry(right, textvariable=self.fields["api_key"], show="*").pack(fill="x")
        ttk.Label(right, text="思考参数接口（portable 为通用策略；原生接口须由模型支持）").pack(anchor="w", pady=(8, 4))
        ttk.Combobox(right, textvariable=self.reasoning_api, values=REASONING_APIS,
                     state="readonly").pack(fill="x")
        ttk.Label(right, text="原生 effort 上限（按模型能力选择；anthropic_manual 为旧版预算接口）").pack(anchor="w", pady=(8, 4))
        ttk.Combobox(right, textvariable=self.effort_ceiling, values=EFFORT_CEILINGS,
                     state="readonly").pack(fill="x")
        ttk.Label(right, text="支持的思考等级（决定思考菜单中的可选档位）").pack(anchor="w", pady=(10, 4))
        levels_row = ttk.Frame(right)
        levels_row.pack(fill="x")
        for level in THINKING_LEVELS:
            ttk.Checkbutton(levels_row, text=level, variable=self.level_vars[level]).pack(side="left", padx=(0, 10))

        self.test_status = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.test_status, foreground=theme.muted, padding=(18, 2)).pack(anchor="w")
        footer = ttk.Frame(self, padding=18)
        footer.pack(fill="x")
        self.test_button = ttk.Button(footer, text="测试真实连接", command=self.test_connection)
        self.test_button.pack(side="left")
        ttk.Button(footer, text="保存模型", command=self.save_entry).pack(side="right", padx=(8, 0))
        ttk.Button(footer, text="设为当前模型", command=self.activate).pack(side="right", padx=(8, 0))
        ttk.Button(footer, text="保存并关闭", style="Primary.TButton", command=self.save_and_close).pack(side="right")
        self.refresh()
        self.listbox.selection_set(self.selected)
        self.choose()

    def refresh(self):
        self.listbox.delete(0, "end")
        for item in self.items:
            marker = "● " if item["id"] == self.manager.active_id else "○ "
            self.listbox.insert("end", marker + item["label"])

    def _form_item(self, model_id=None):
        levels = [level for level in THINKING_LEVELS if self.level_vars[level].get()]
        if not levels:
            raise ValueError("至少选择一个支持的思考等级。")
        return {"id": model_id or uuid.uuid4().hex, "label": self.label_var.get(),
                "model": self.fields["model"].get(), "provider": self.fields["provider"].get(),
                "base_url": self.fields["base_url"].get(), "api_key": self.fields["api_key"].get(),
                "reasoning_api": self.reasoning_api.get(), "effort_ceiling": self.effort_ceiling.get(),
                "levels": levels}

    def choose(self, _event=None):
        selected = self.listbox.curselection()
        if not selected:
            return
        self.selected = selected[0]
        item = self.items[self.selected]
        self.label_var.set(item["label"])
        self.fields["model"].set(item["model"])
        self.fields["provider"].set(item["provider"])
        self.fields["base_url"].set(item["base_url"])
        self.fields["api_key"].set(item["api_key"])
        self.reasoning_api.set(item["reasoning_api"])
        self.effort_ceiling.set(item["effort_ceiling"])
        for level, var in self.level_vars.items():
            var.set(level in item["levels"])

    def new(self):
        self.items.append({"id": uuid.uuid4().hex, "label": "新模型", "model": "",
                           "provider": "openai", "base_url": "https://api.openai.com/v1",
                           "api_key": "", "reasoning_api": "portable", "effort_ceiling": "high",
                           "levels": list(THINKING_LEVELS)})
        self.selected = len(self.items) - 1
        self.refresh()
        self.listbox.selection_clear(0, "end")
        self.listbox.selection_set(self.selected)
        self.choose()

    def delete(self):
        if len(self.items) == 1:
            messagebox.showerror("无法删除", "至少需要保留一个模型。", parent=self)
            return
        item = self.items[self.selected]
        if item["id"] == self.manager.active_id:
            messagebox.showerror("无法删除", "不能删除当前正在使用的模型，请先切换到其他模型。", parent=self)
            return
        remaining = [entry for entry in self.items if entry["id"] != item["id"]]
        try:
            self.manager.replace(remaining, self.manager.active_id)
        except (ValueError, OSError) as exc:
            messagebox.showerror("删除失败", str(exc), parent=self)
            return
        self.selected = min(self.selected, len(remaining) - 1)
        self.items = self.manager.all()
        self.refresh()
        self.listbox.selection_set(self.selected)
        self.choose()
        self.app.populate_model_menu()

    def show_model_menu(self, event):
        if event.keysym == "F10":
            selected = self.listbox.curselection()
            if not selected:
                return "break"
            index = selected[0]
            x, y, _width, height = self.listbox.bbox(index)
            menu_x, menu_y = self.listbox.winfo_rootx() + x, self.listbox.winfo_rooty() + y + height
        else:
            index = self.listbox.nearest(event.y)
            bounds = self.listbox.bbox(index)
            if not bounds or not bounds[1] <= event.y < bounds[1] + bounds[3]:
                return "break"
            menu_x, menu_y = event.x_root, event.y_root
        self.listbox.selection_clear(0, "end")
        self.listbox.selection_set(index)
        self.listbox.activate(index)
        self.choose()
        deletable = len(self.items) > 1 and self.items[index]["id"] != self.manager.active_id
        self.model_context_menu.entryconfigure("删除", state="normal" if deletable else "disabled")
        try:
            self.model_context_menu.tk_popup(menu_x, menu_y)
        finally:
            self.model_context_menu.grab_release()
        return "break"

    def rename(self):
        item = self.items[self.selected]
        name = simpledialog.askstring("重命名模型", "显示名称（1–60 个字符）", initialvalue=item["label"], parent=self)
        if name is None:
            return
        renamed = {**item, "label": name}
        try:
            self.manager.replace([renamed if entry["id"] == item["id"] else entry for entry in self.items],
                                 self.manager.active_id)
        except (ValueError, OSError) as exc:
            messagebox.showerror("重命名失败", str(exc), parent=self)
            return
        self.items = self.manager.all()
        self.label_var.set(self.items[self.selected]["label"])
        self.refresh()
        self.listbox.selection_set(self.selected)
        self.app.refresh_model_ui()
        self.app.populate_model_menu()

    def save_entry(self) -> bool:
        try:
            item = self._form_item(self.items[self.selected]["id"])
        except ValueError as exc:
            messagebox.showerror("保存失败", str(exc), parent=self)
            return False
        self.items[self.selected] = item
        try:
            self.manager.replace(self.items, self.manager.active_id)
        except ValueError as exc:
            messagebox.showerror("保存失败", str(exc), parent=self)
            return False
        self.items = self.manager.all()
        self.refresh()
        self.listbox.selection_set(self.selected)
        return True

    def activate(self):
        if not self.save_entry():
            return
        try:
            config = self.build_config()
        except (ValueError, PolicyError) as exc:
            messagebox.showerror("切换失败", str(exc), parent=self)
            return
        try:
            self.app.apply_config(config)
        except (ValueError, PolicyError) as exc:
            messagebox.showerror("切换失败", str(exc), parent=self)
            return
        self.items = self.manager.all()
        self.refresh()
        self.listbox.selection_set(self.selected)
        self.app.chat("配置", f"已切换到模型「{self.items[self.selected]['label']}」。")

    def build_config(self):
        item = self.items[self.selected]
        if not self.fields["model"].get().strip():
            raise ValueError("请填写服务商提供的模型名称。")
        timeout = int(self.timeout.get())
        if not 25 <= timeout <= 600:
            raise ValueError("API 等待时限必须在 25–600 秒之间。")
        config = replace(self.app.config, provider=self.fields["provider"].get(),
                         base_url=self.fields["base_url"].get(), model=self.fields["model"].get(),
                         api_key=self.fields["api_key"].get(), audit_model=self.fields["audit_model"].get(),
                         reasoning_api=self.reasoning_api.get(),
                         effort_ceiling=self.effort_ceiling.get(), timeout=timeout,
                         active_model_id=item["id"], quick_hotkey=self.quick_hotkey.get())
        config.validate()
        return config

    def test_connection(self):
        try:
            config = self.build_config()
        except (ValueError, PolicyError) as exc:
            messagebox.showerror("配置无效", str(exc), parent=self)
            return
        self.test_button.configure(state="disabled")
        self.test_status.set("正在请求模型…")
        cancel = threading.Event()
        def worker():
            try:
                reply = AgentLLMClient(config, cancel).probe()
                self.app.events.put(("connection_test", (self, True, "连接成功 · " + reply)))
            except Exception as exc:
                self.app.events.put(("connection_test", (self, False, str(exc))))
        threading.Thread(target=worker, daemon=True, name="ClawX-Connection-Test").start()

    def save_and_close(self):
        try:
            item = self._form_item(self.items[self.selected]["id"])
        except ValueError as exc:
            messagebox.showerror("保存失败", str(exc), parent=self)
            return
        self.items[self.selected] = item
        try:
            self.manager.replace(self.items, item["id"])
        except ValueError as exc:
            messagebox.showerror("保存失败", str(exc), parent=self)
            return
        try:
            config = self.build_config()
        except (ValueError, PolicyError) as exc:
            messagebox.showerror("保存失败", str(exc), parent=self)
            return
        try:
            self.app.apply_config(config)
            self.app.set_theme(self.theme_var.get())
        except (ValueError, PolicyError) as exc:
            messagebox.showerror("保存失败", str(exc), parent=self)
            return
        if not config.api_key:
            self.app.chat("配置", "模型已保存，但尚未填写 API Key；填写并再次保存后即可真实对话。")
        else:
            self.app.chat("配置", "模型配置已更新，将在下一个任务生效。")
        self.destroy()


class BranchDialog(tk.Toplevel):
    """Choose an inclusive branch point without modifying the source conversation."""

    def __init__(self, app, record):
        super().__init__(app.root)
        self.app, self.record = app, record
        self.title("ClawX · 创建分支对话")
        self.geometry("840x580")
        self.minsize(640, 440)
        self.configure(bg=theme.bg)
        self.transient(app.root)
        self.grab_set()
        body = ttk.Frame(self, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="选择分支点，新对话将包含该消息及之前的上下文。", wraplength=760).pack(anchor="w")
        ttk.Label(body, text="原对话会保留；此操作不会重新执行工具，也不会复制或回滚项目文件。",
                  foreground=theme.muted, wraplength=760).pack(anchor="w", pady=(4, 12))
        self.title_var = tk.StringVar(value=(record["title"] or "未命名对话")[:75] + " · 分支")
        ttk.Entry(body, textvariable=self.title_var).pack(fill="x", pady=(0, 12))
        panes = ttk.Panedwindow(body, orient="horizontal")
        panes.pack(fill="both", expand=True)
        left, right = ttk.Frame(panes), ttk.Frame(panes)
        panes.add(left, weight=1)
        panes.add(right, weight=2)
        self.message_list = tk.Listbox(left, bg=theme.field, fg=theme.fg, selectbackground=theme.select,
                                       exportselection=False, relief="flat", highlightthickness=0,
                                       font=(UI_FONT, 10))
        scrollbar = ttk.Scrollbar(left, command=self.message_list.yview)
        scrollbar.pack(side="right", fill="y")
        self.message_list.configure(yscrollcommand=scrollbar.set)
        self.message_list.pack(fill="both", expand=True, padx=(0, 8))
        self.preview = text_box(right, wrap="word")
        self.message_list.bind("<<ListboxSelect>>", self.preview_message)
        for index, message in enumerate(record["messages"]):
            role = "你" if message["role"] == "user" else "ClawX"
            excerpt = " ".join(message["content"].split())[:44]
            self.message_list.insert("end", f"{index + 1}. {role} · {excerpt}")
        self.message_list.selection_set(len(record["messages"]) - 1)
        self.message_list.see("end")
        self.preview_message()
        footer = ttk.Frame(body)
        footer.pack(fill="x", pady=(14, 0))
        ttk.Button(footer, text="创建分支", style="Primary.TButton", command=self.create).pack(side="right")
        ttk.Button(footer, text="取消", command=self.destroy).pack(side="right", padx=8)
        self.bind("<Escape>", lambda _event: self.destroy())

    def preview_message(self, _event=None):
        selected = self.message_list.curselection()
        if selected:
            set_text(self.preview, self.record["messages"][selected[0]]["content"])

    def create(self):
        selected = self.message_list.curselection()
        if not selected:
            return
        try:
            self.app.create_branch(self.record["id"], selected[0], self.title_var.get())
        except (ValueError, OSError, PolicyError) as exc:
            messagebox.showerror("分支创建失败", str(exc), parent=self)
            return
        self.destroy()


class QuickComposer(tk.Toplevel):
    """A reusable, frameless composer: one input and exactly two buttons."""

    def __init__(self, app):
        super().__init__(app.root)
        self.withdraw()
        self.app = app
        self._focus_after = None
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.panel = tk.Frame(self, highlightthickness=1, borderwidth=0)
        self.panel.pack(fill="both", expand=True)
        self.input = tk.Text(self.panel, height=2, width=40, wrap="word", relief="flat",
                             borderwidth=0, highlightthickness=0, font=(UI_FONT, 12),
                             padx=12, pady=13, undo=True)
        self.input.pack(side="left", fill="both", expand=True, padx=(5, 0), pady=5)
        self.full_button = tk.Button(self.panel, text="进入完整页面", command=self.open_full,
                                     relief="flat", borderwidth=0, font=(UI_FONT, 9),
                                     padx=10, pady=10, cursor="hand2")
        self.full_button.pack(side="right", padx=(4, 12), pady=16)
        self.send_button = tk.Button(self.panel, text="发送", command=self.submit,
                                     relief="flat", borderwidth=0, font=(UI_FONT, 10, "bold"),
                                     padx=14, pady=10, cursor="hand2")
        self.send_button.pack(side="right", padx=(8, 0), pady=16)
        self.input.bind("<Return>", self.submit)
        self.input.bind("<Control-Return>", self.submit)
        self.input.bind("<Shift-Return>", self.newline)
        self.bind("<Escape>", lambda _event: self.hide())
        self.bind("<FocusOut>", self._focus_out)
        self.apply_theme()

    def apply_theme(self):
        self.configure(bg=theme.border)
        self.panel.configure(bg=theme.panel, highlightbackground=theme.border)
        self.input.configure(bg=theme.panel, fg=theme.fg, insertbackground=theme.fg,
                             selectbackground=theme.text_select)
        self.full_button.configure(bg=theme.panel, fg=theme.muted,
                                   activebackground=theme.field, activeforeground=theme.fg)
        self.send_button.configure(bg=theme.accent, fg="white", disabledforeground=theme.muted,
                                   activebackground=theme.select, activeforeground=theme.fg)

    def show(self):
        self.apply_theme()
        self.set_busy(self.app.ui_busy or self.app.agent.running)
        width = min(720, self.winfo_screenwidth() - 50)
        height = 92
        x = max(0, (self.winfo_screenwidth() - width) // 2)
        y = max(0, int(self.winfo_screenheight() * 0.30))
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.deiconify()
        self.lift()
        self.update_idletasks()
        self.input.focus_force()

    def set_busy(self, busy):
        self.send_button.configure(state="disabled" if busy else "normal")

    def hide(self):
        if self._focus_after:
            self.after_cancel(self._focus_after)
            self._focus_after = None
        self.withdraw()
        return "break"

    def _focus_out(self, _event=None):
        if self.state() != "withdrawn" and self._focus_after is None:
            self._focus_after = self.after(100, self._check_focus)

    def _check_focus(self):
        self._focus_after = None
        focus = self.focus_displayof()
        if focus is None or not str(focus).startswith(str(self) + "."):
            self.hide()

    def newline(self, _event=None):
        if self.input.tag_ranges("sel"):
            self.input.delete("sel.first", "sel.last")
        self.input.insert("insert", "\n")
        return "break"

    def submit(self, _event=None):
        if not self.app.ui_busy and not self.app.agent.running and self.input.get("1.0", "end-1c").strip():
            self.app.open_from_quick(send=True)
        return "break"

    def open_full(self):
        self.app.open_from_quick(send=False)

    def destroy(self):
        self.hide()
        super().destroy()


class ClawXApp(DashboardLayout):
    def __init__(self, root: tk.Tk, base_dir: Path | None = None):
        self.root = root
        self.root.title("ClawX · Local Agent Studio")
        width = min(1480, root.winfo_screenwidth() - 70)
        height = min(940, root.winfo_screenheight() - 90)
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(1050, 680)
        self.root.configure(bg=theme.bg)
        self.base_dir = base_dir or Path(__file__).resolve().parent.parent
        self.data_dir = self.base_dir / ".clawx-data"
        self.data_dir.mkdir(exist_ok=True)
        self.default_workspace = self.base_dir / "workspace"
        self.default_workspace.mkdir(exist_ok=True)
        self.config = default_config()
        startup_error = ""
        try:
            self.projects = ProjectManager(self.data_dir / "projects.json", self.default_workspace)
        except (ValueError, OSError, TypeError) as exc:
            self.projects = ProjectManager(self.data_dir / "projects-recovered.json", self.default_workspace)
            startup_error = f"项目配置读取失败，原文件保留：{exc}"
        settings = self.data_dir / "settings.json"
        if settings.exists():
            try:
                data = json.loads(settings.read_text(encoding="utf-8"))
                allowed = {key: value for key, value in data.items() if key in self.config.public()}
                # Validate persisted settings rather than accepting arbitrary types.
                for key in ("provider", "base_url", "model", "audit_model", "work_mode", "thinking_level",
                            "chat_style", "theme", "active_model_id"):
                    if key in allowed and not isinstance(allowed[key], str):
                        raise ValueError(f"{key} 必须是字符串")
                timeout = allowed.get("timeout", 120)
                if type(timeout) is not int or not 25 <= timeout <= 600:
                    raise ValueError("timeout 必须为 25–600 秒整数")
                if allowed.get("provider", "openai") not in {"openai", "anthropic"}:
                    raise ValueError("provider 无效")
                if allowed.get("work_mode", "work") not in VALID_WORK_MODES:
                    raise ValueError("work_mode 无效")
                if allowed.get("thinking_level", "Medium") not in VALID_THINKING_LEVELS:
                    raise ValueError("thinking_level 无效")
                if allowed.get("theme", "dark") not in {"dark", "light", "system"}:
                    raise ValueError("theme 无效")
                if "chat_style" not in allowed:
                    allowed["chat_style"] = "instant" if allowed.get("thinking_level", "Medium") == "Low" else "deep"
                candidate = replace(self.config, **allowed)
                candidate.validate()
                self.config = candidate
            except (ValueError, TypeError, AttributeError, PolicyError) as exc:
                startup_error = f"配置无效，使用默认配置：{exc}"
        active_project = self.projects.active()
        # 模型列表：默认条目继承当前配置；每个模型声明支持的思考等级。
        default_model = {"id": uuid.uuid4().hex, "label": (self.config.model or "本地演示"),
                         "model": self.config.model, "provider": self.config.provider,
                         "base_url": self.config.base_url, "api_key": self.config.api_key,
                         "reasoning_api": self.config.reasoning_api,
                         "effort_ceiling": self.config.effort_ceiling,
                         "levels": list(THINKING_LEVELS)}
        try:
            self.models = ModelRegistry(self.data_dir / "models.json", default_model)
        except (ValueError, OSError, TypeError) as exc:
            self.models = ModelRegistry(self.data_dir / "models-recovered.json", default_model)
            startup_error += f"\n模型列表读取失败，原文件保留：{exc}"
        entry = self.models.by_id(self.config.active_model_id) or self.models.active()
        if self.config.active_model_id != entry["id"]:
            self.config = replace(self.config, active_model_id=entry["id"])
        supported = [level for level in THINKING_LEVELS if level in entry.get("levels", THINKING_LEVELS)]
        if self.config.thinking_level not in supported:
            self.config = replace(self.config, thinking_level=nearest_level(self.config.thinking_level, supported))
        if entry.get("api_key") and not self.config.api_key:
            self.config = replace(self.config, api_key=entry["api_key"])
        if not self.config.api_key:
            try:
                hostname = urllib.parse.urlsplit(self.config.base_url).hostname
            except ValueError:
                hostname = None
            if hostname not in {"localhost", "127.0.0.1", "::1"}:
                startup_error += "\n尚未填写 API Key；请在「模型与连接」中填写密钥后使用。"
        self.state = PolicyState(
            active_project["root"], allowed_roots=tuple(active_project["allowed_folders"]),
            project_name=active_project["name"], project_prompt=active_project["prompt"],
            work_mode=self.config.work_mode,
            thinking_level=self.config.thinking_level,
        )
        try:
            self.skills = SkillManager(self.data_dir / "skills.json")
        except (ValueError, OSError) as exc:
            # Preserve malformed data; allow the user to repair/import through the manager.
            self.skills = SkillManager(self.data_dir / "skills-recovered.json")
            startup_error += f"\nSkill 配置读取失败，原文件保留：{exc}"
        try:
            self.conversations = ConversationManager(self.data_dir / "conversations.json")
        except (ValueError, OSError, TypeError) as exc:
            self.conversations = ConversationManager(self.data_dir / "conversations-recovered.json")
            startup_error += f"\n对话记录读取失败，原文件保留：{exc}"
        self.session_id: str | None = None
        self.session_created = ""
        self.session_messages: list[dict] = []
        self._task_public_replies: list[str] = []
        self._stream_active = False
        self._stream_buffer = ""
        self._token_pending = ""
        self.events = queue.Queue()
        self.quick_events = queue.Queue()
        self.quick_window = None
        self.wake_hotkey = GlobalHotkey(self.quick_events, self.config.quick_hotkey)
        self.agent = AgentController(self.state, self.skills, self.events)
        self.review: ReviewDialog | None = None
        self.rows = {}
        self.report = ""
        self.closing = False
        self.message_index = 0
        self.chat_log = []
        self.console_log = []
        self._execution_started = None
        self._execution_header_tag = None
        self._execution_label = ""
        self._applied_theme = self._resolved_theme()
        theme.apply(self._applied_theme)
        self._style()
        self._build()
        self._window_chrome()
        self.refresh_model_ui()
        self.refresh_conversations_ui()
        self.wake_hotkey.start()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(40, self.pump)
        self.root.after(2000, self._poll_system_theme)
        if startup_error:
            self.welcome.place_forget()
            self.chat("配置", startup_error)

    def _window_chrome(self):
        self.window_icon = tk.PhotoImage(master=self.root, width=32, height=32)
        self.window_icon.put(theme.sidebar, to=(0, 0, 32, 32))
        for offset in range(9):
            for width in range(4):
                for x, y in ((5 + offset, 7 + offset), (5 + offset, 23 - offset),
                             (18 + offset, 15 - offset), (18 + offset, 15 + offset)):
                    self.window_icon.put(theme.accent, (min(31, x), min(31, y + width)))
        self.root.iconphoto(True, self.window_icon)
        if os.name != "nt":
            return
        # Optional native decoration. Unsupported Windows builds keep OS defaults.
        import ctypes
        from ctypes import wintypes
        try:
            self.root.update_idletasks()
            parent = ctypes.windll.user32.GetParent
            parent.argtypes, parent.restype = [wintypes.HWND], wintypes.HWND
            hwnd = parent(self.root.winfo_id())
            setter = ctypes.windll.dwmapi.DwmSetWindowAttribute
            setter.argtypes = [wintypes.HWND, wintypes.DWORD, wintypes.LPCVOID, wintypes.DWORD]
            setter.restype = ctypes.c_long
            enabled = ctypes.c_int(1)
            setter(hwnd, 20, ctypes.byref(enabled), ctypes.sizeof(enabled))
            for attribute, color in ((35, theme.bg), (36, theme.fg)):
                red, green, blue = (int(color[i:i+2], 16) for i in (1, 3, 5))
                native_color = wintypes.DWORD(red | green << 8 | blue << 16)
                setter(hwnd, attribute, ctypes.byref(native_color), ctypes.sizeof(native_color))
        except (OSError, AttributeError):
            return

    def show_quick_composer(self):
        if self.closing:
            return
        modal = self.root.grab_current()
        if modal is not None:
            self.root.deiconify()
            modal.lift()
            modal.focus_force()
            return
        if self.quick_window is None or not self.quick_window.winfo_exists():
            self.quick_window = QuickComposer(self)
        self.quick_window.show()

    def open_from_quick(self, *, send):
        if self.quick_window is None:
            return
        draft = self.quick_window.input.get("1.0", "end-1c")
        self.quick_window.hide()
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        if send and not self.ui_busy and not self.agent.running:
            self.set_work_mode("chat")
        current = self.input.get("1.0", "end-1c")
        previous_draft = ""
        if send and not self.ui_busy and not self.agent.running and draft.strip():
            previous_draft = current if current != draft else ""
            self.input.delete("1.0", "end")
            self.input.insert("1.0", draft)
        elif draft.strip() and draft != current:
            if current:
                self.input.insert("end-1c", "\n")
            self.input.insert("end-1c", draft)
        self.quick_window.input.delete("1.0", "end")
        self._update_placeholder()
        self.input.focus_set()
        if send:
            self.send()
            if previous_draft:
                # Only the quick question is sent; keep any existing full-page
                # draft available for editing rather than submitting it silently.
                if self.input.get("1.0", "end-1c"):
                    self.input.insert("end-1c", "\n")
                self.input.insert("end-1c", previous_draft)
                self._update_placeholder()

    def chat(self, role, text):
        if role in {"思考", "运行策略"}:
            return
        if role == "ClawX" and getattr(self, "ui_busy", False):
            self._task_public_replies.append(text)
        self.message_index += 1
        self.chat_log.append((role, text))
        if len(self.chat_log) > 4000:
            del self.chat_log[:1500]
        self.chat_text.configure(state="normal")
        if role == "执行时间":
            self.chat_text.insert("end", "────────────────────────────────\n", "execution_rule")
            self._execution_header_tag = f"execution_{self.message_index}"
            self._execution_label = text
            self.chat_text.insert("end", text, ("execution_heading", self._execution_header_tag))
            self.chat_text.insert("end", "\n", "execution_heading")
        elif role in {"工具", "结果", "计划"}:
            details = text
            try:
                data = json.loads(text)
                details = json.dumps(data, ensure_ascii=False, indent=2)
                name = data.get("tool", "未知工具") if isinstance(data, dict) else "未知工具"
            except (ValueError, TypeError, KeyError, AttributeError):
                name = "未知工具"
            # Tool names are model data; keep the compact row on a single line.
            name = " ".join(str(name).split())[:64]
            if role == "工具":
                display = f"【工具调用】{name}  ›"
            elif role == "结果":
                display = "执行结果 ›"
            else:
                display = "【计划】" + " ".join(text.split())[:96] + "  ›"
            link = f"request_{self.message_index}"
            title = "工具请求参数 · " + name if role == "工具" else "执行结果" if role == "结果" else "执行计划"
            self.chat_text.tag_configure(link, foreground=theme.tool_fg if role == "工具" else theme.muted)
            self.chat_text.tag_bind(link, "<Button-1>", lambda _e, value=details, heading=title: self._show_text(heading, value))
            self.chat_text.tag_bind(link, "<Enter>", lambda _e: self.chat_text.configure(cursor="hand2"))
            self.chat_text.tag_bind(link, "<Leave>", lambda _e: self.chat_text.configure(cursor="xterm"))
            self.chat_text.insert("end", display + "\n", ("tool", link))
        else:
            # Give public replies and errors their own spacing and hierarchy.
            heading = {"用户": "你", "ClawX": "✳  ClawX"}.get(role, role)
            self.chat_text.insert("end", heading + "\n", "heading")
            if role in {"用户", "ClawX"}:
                render_markdown(self.chat_text, text, base="user" if role == "用户" else "assistant")
                self.chat_text.insert("end", "\n")
            else:
                self.chat_text.insert("end", text + "\n\n", "error" if role in {"拦截", "错误"} else "meta")
        if int(self.chat_text.index("end-1c").split(".")[0]) > 6000:
            self.chat_text.delete("1.0", "1500.0")
        self.chat_text.configure(state="disabled")
        self.chat_text.see("end")

    @staticmethod
    def _format_elapsed(seconds):
        seconds = max(0.0, float(seconds))
        if seconds < 1:
            return "<1s"
        hours, rest = divmod(int(seconds), 3600)
        minutes, secs = divmod(rest, 60)
        parts = ([f"{hours}h"] if hours else []) + ([f"{minutes}min"] if minutes else [])
        if secs or not parts:
            parts.append(f"{secs}s")
        return " ".join(parts)

    def _set_execution_time(self, elapsed, *, finished=False):
        label = ("执行了 " if finished else "执行中 · ") + self._format_elapsed(elapsed)
        if label == self._execution_label or not self._execution_header_tag:
            return
        ranges = self.chat_text.tag_ranges(self._execution_header_tag)
        if ranges:
            self.chat_text.configure(state="normal")
            start = str(ranges[0])
            self.chat_text.delete(start, ranges[1])
            self.chat_text.insert(start, label, ("execution_heading", self._execution_header_tag))
            self.chat_text.configure(state="disabled")
        for index in range(len(self.chat_log)-1, -1, -1):
            if self.chat_log[index][0] == "执行时间":
                self.chat_log[index] = ("执行时间", label)
                break
        self._execution_label = label

    def _open_config(self):
        ConfigDialog(self)

    def _open_skills(self):
        SkillDialog(self)

    def _open_projects(self):
        if self.agent.running or self.ui_busy:
            self.status.set("请先完成或停止当前任务，再管理项目")
            return
        ProjectDialog(self)

    def activate_project(self, project_id):
        if self.agent.running or self.ui_busy:
            raise PolicyError("任务执行期间不能切换项目。")
        self._end_session()
        project = self.projects.select(project_id)
        self.close_thinking_menu()
        self.state.update(root=project["root"], allowed_roots=project["allowed_folders"],
                          project_name=project["name"], project_prompt=project["prompt"])
        if self.review:
            self.review.finish(False)
        self.agent.histories = {"work": [], "chat": []}
        self.new_task()
        self.refresh_workspace_ui()
        self.chat("项目", f"已切换到「{project['name']}」。主目录：{project['root']}；额外允许目录：{len(project['allowed_folders'])} 个。")
        self.status.set("当前项目：" + project["name"])
        self.refresh_conversations_ui()

    def _display_path(self, value):
        try:
            return str(Path(value).relative_to(self.state.root))
        except (ValueError, TypeError):
            return value

    def _persist_settings(self):
        try:
            path = self.data_dir / "settings.json"
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps(self.config.public(), ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp, path)
        except OSError as exc:
            self.status.set(f"偏好已应用，本次未能保存到磁盘：{exc}")

    # ---- 模型列表 -----------------------------------------------------------

    def _active_levels(self):
        entry = self.models.by_id(self.config.active_model_id) if hasattr(self, "models") else None
        if entry and entry.get("levels"):
            return [level for level in THINKING_LEVELS if level in entry["levels"]]
        return list(THINKING_LEVELS)

    def select_model(self, model_id):
        if self.agent.running or self.ui_busy:
            raise PolicyError("任务执行期间不能切换模型。")
        entry = self.models.by_id(model_id)
        if not entry:
            raise ValueError("模型不存在。")
        supported = [level for level in THINKING_LEVELS if level in entry["levels"]]
        level = (self.config.thinking_level if self.config.thinking_level in supported
                 else nearest_level(self.config.thinking_level, supported))
        self.config = replace(self.config, model=entry["model"], provider=entry["provider"],
                              base_url=entry["base_url"], api_key=entry["api_key"],
                              reasoning_api=entry["reasoning_api"], effort_ceiling=entry["effort_ceiling"],
                              active_model_id=model_id, thinking_level=level)
        self.state.update(thinking_level=level)
        self.models.set_active(model_id)
        self._persist_settings()
        self.refresh_model_ui()
        self.populate_model_menu()
        self._refresh_mode_and_thinking_styles()
        self.status.set("当前模型：" + entry["label"])

    def apply_config(self, config: LLMConfig):
        """应用完整配置：当前模型条目、密钥、超时、审查模型等全部生效。"""
        if self.agent.running or self.ui_busy:
            raise PolicyError("任务执行期间不能切换模型。")
        entry = self.models.by_id(config.active_model_id) or self.models.active()
        supported = [level for level in THINKING_LEVELS if level in entry.get("levels", THINKING_LEVELS)]
        if config.thinking_level not in supported:
            config = replace(config, thinking_level=nearest_level(config.thinking_level, supported))
        self.models.set_active(entry["id"])
        self.config = replace(config, active_model_id=entry["id"])
        if self.wake_hotkey.shortcut != self.config.quick_hotkey:
            self.wake_hotkey.configure(self.config.quick_hotkey)
        self.state.update(thinking_level=self.config.thinking_level)
        self._persist_settings()
        self.refresh_model_ui()
        self.populate_model_menu()
        self._refresh_mode_and_thinking_styles()
        self.status.set("当前模型：" + entry["label"])

    # ---- 对话存储 -----------------------------------------------------------

    def _now(self) -> str:
        return datetime.now().isoformat(timespec="seconds")

    def _append_session(self, role, content, mode):
        self.session_messages.append({"role": role, "content": content, "mode": mode})

    def _save_conversation(self):
        if not self.session_messages or not hasattr(self, "conversations"):
            return
        title = next((m["content"].splitlines()[0][:60] for m in self.session_messages
                      if m["role"] == "user"), "未命名对话")
        existing = self.conversations.by_id(self.session_id)
        metadata = {}
        if existing and "parent_id" in existing:
            title = existing["title"]
            metadata = {key: existing[key] for key in ("parent_id", "branch_at")}
        project = self.projects.active()
        self.conversations.upsert({
            "id": self.session_id, "project_id": project["id"], "title": title,
            "created": self.session_created, "updated": self._now(),
            "messages": self.session_messages[-200:],
            **metadata,
        })
        self.refresh_conversations_ui()

    def _end_session(self):
        self._save_conversation()
        self.session_id, self.session_created, self.session_messages = None, "", []

    def _record_done(self):
        mode = self.state.work_mode
        replies = self._task_public_replies or [m["content"] for m in self.agent.histories.get(mode, [])[-2:]
                                               if m["role"] == "assistant"]
        for content in replies:
            self._append_session("assistant", content[:30000], mode)
        self._task_public_replies = []
        self._save_conversation()

    def refresh_conversations_ui(self):
        if not hasattr(self, "conv_listbox"):
            return
        project = self.projects.active()
        records = self.conversations.all(project["id"]) if hasattr(self, "conversations") else []
        self.conv_listbox.delete(0, "end")
        self.conv_map = {}
        for index, record in enumerate(records):
            self.conv_map[index] = record["id"]
            prefix = "↳ " if "parent_id" in record else ""
            self.conv_listbox.insert("end", prefix + (record["title"] or "未命名对话")[:24])
            if record["id"] == self.session_id:
                self.conv_listbox.selection_set(index)
        if hasattr(self, "project_combo"):
            self.project_combo.configure(values=[item["name"] for item in self.projects.all()])
            self.project_var.set(project["name"])

    def _conversation_selected(self, _event=None):
        selection = self.conv_listbox.curselection()
        if not selection:
            return
        record_id = self.conv_map.get(selection[0])
        if record_id:
            self.load_conversation(record_id)

    def _conversation_deleted(self):
        selection = self.conv_listbox.curselection()
        if not selection:
            self.status.set("请先在对话记录中选择一条记录")
            return
        record_id = self.conv_map.get(selection[0])
        if not record_id:
            return
        if self.agent.running or self.ui_busy:
            self.status.set("请先完成或停止当前任务，再删除对话记录")
            return
        self.conversations.delete(record_id)
        if self.session_id == record_id:
            self.session_id, self.session_created, self.session_messages = None, "", []
        self.refresh_conversations_ui()
        self.status.set("已删除所选对话记录")

    def _conversation_context_menu(self, event):
        index = self.conv_listbox.nearest(event.y)
        bounds = self.conv_listbox.bbox(index)
        if not bounds or not bounds[1] <= event.y < bounds[1] + bounds[3]:
            return "break"
        record_id = self.conv_map.get(index)
        if not record_id:
            return "break"
        self.conv_listbox.selection_clear(0, "end")
        self.conv_listbox.selection_set(index)
        self.conv_listbox.activate(index)
        menu = self.conv_context_menu
        menu.entryconfigure("创建分支对话…", command=lambda: self._open_branch_dialog(record_id),
                            state="disabled" if self.agent.running or self.ui_busy else "normal")
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _open_branch_dialog(self, record_id=None):
        if self.agent.running or self.ui_busy:
            self.status.set("请先完成或停止当前任务，再创建分支")
            return
        if self.session_id:
            self._save_conversation()
        if record_id is None:
            record_id = self.session_id
            if record_id is None and self.conv_listbox.curselection():
                record_id = self.conv_map.get(self.conv_listbox.curselection()[0])
        record = self.conversations.by_id(record_id)
        if not record or not record["messages"]:
            self.status.set("请先开始对话，或在历史记录中选择需要分支的对话")
            return
        if record["project_id"] != self.projects.active_id:
            self.status.set("请先切换到该对话所属项目")
            return
        return BranchDialog(self, record)

    def create_branch(self, record_id, message_index, title=None):
        if self.agent.running or self.ui_busy:
            raise PolicyError("请先完成或停止当前任务，再创建分支。")
        source = self.conversations.by_id(record_id)
        if not source or source["project_id"] != self.projects.active_id:
            raise PolicyError("只能为当前项目中存在的对话创建分支。")
        self._save_conversation()
        branch = self.conversations.fork(record_id, message_index, title)
        self.load_conversation(branch["id"])
        self.status.set("已创建分支：" + branch["title"])
        return branch

    def _sidebar_project_changed(self, _event=None):
        name = self.project_var.get()
        project = next((item for item in self.projects.all() if item["name"] == name), None)
        if not project or project["id"] == self.projects.active_id:
            return
        try:
            self.activate_project(project["id"])
        except PolicyError as exc:
            self.status.set(str(exc))
            self.refresh_conversations_ui()

    def load_conversation(self, record_id):
        if self.agent.running or self.ui_busy:
            self.status.set("请先完成或停止当前任务，再加载历史对话")
            return
        record = self.conversations.by_id(record_id)
        if not record:
            return
        if record["project_id"] != self.projects.active_id:
            self.status.set("该对话属于其他项目，请先切换项目")
            return
        self._end_session()
        if record["messages"]:
            self.set_work_mode(record["messages"][-1]["mode"])
        self.session_id, self.session_created = record["id"], record["created"]
        self.session_messages = list(record["messages"])
        self.agent.histories["work"] = [{"role": m["role"], "content": m["content"]}
                                        for m in record["messages"] if m["mode"] == "work"][-8:]
        self.agent.histories["chat"] = [{"role": m["role"], "content": m["content"]}
                                        for m in record["messages"] if m["mode"] == "chat"][-8:]
        set_text(self.chat_text, "")
        self.chat_log = []
        self.message_index = 0
        for message in record["messages"]:
            self.chat("用户" if message["role"] == "user" else "ClawX", message["content"])
        self.welcome.place_forget()
        title = record["title"] or "历史对话"
        self.task_title.configure(text=title)
        self.sidebar_task.set(title)
        self.status.set("已加载对话：" + title)
        self.refresh_conversations_ui()

    def populate_model_menu(self):
        if not hasattr(self, "model_combo"):
            return
        entries = self.models.all()
        active = self.models.by_id(self.models.active_id) or entries[0]
        self.model_combo.configure(values=[item["label"] for item in entries])
        self.model_var.set(active["label"])

    def _model_combo_changed(self, _event=None):
        label_text = self.model_var.get()
        entry = next((item for item in self.models.all() if item["label"] == label_text), None)
        if not entry:
            return
        try:
            self.select_model(entry["id"])
        except (ValueError, PolicyError) as exc:
            self.status.set(str(exc))
            self.populate_model_menu()

    # ---- 外观主题 -----------------------------------------------------------

    @staticmethod
    def _system_prefers_light() -> bool:
        if os.name == "nt":
            try:
                import winreg
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                    r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
                    value, _type = winreg.QueryValueEx(key, "AppsUseLightTheme")
                    return bool(value)
            except OSError:
                pass
        return True

    def _resolved_theme(self) -> str:
        if self.config.theme == "system":
            return "light" if self._system_prefers_light() else "dark"
        return self.config.theme

    def set_theme(self, mode: str):
        if mode not in {"dark", "light", "system"}:
            raise ValueError(f"Unknown theme: {mode}")
        if self.agent.running or self.ui_busy:
            self.status.set("请先完成或停止当前任务，再切换外观")
            return
        self.config = replace(self.config, theme=mode)
        self._persist_settings()
        self._apply_theme()

    def _apply_theme(self):
        resolved = self._resolved_theme()
        if getattr(self, "_applied_theme", None) == resolved:
            return
        self._rebuild_ui(resolved)
        self._applied_theme = resolved

    def _poll_system_theme(self):
        if self.closing:
            return
        if self.config.theme == "system":
            self._apply_theme()
        self.root.after(2000, self._poll_system_theme)

    def _rebuild_ui(self, mode):
        theme.apply(mode)
        input_text = self.input.get("1.0", "end-1c")
        chat_log = list(self.chat_log)
        console_log = list(self.console_log)
        rows = list(self.rows.values())
        report = self.report
        welcome_visible = self.welcome.winfo_manager() == "place"
        task_title = self.task_title.cget("text")
        sidebar_task = self.sidebar_task.get()
        tier_sel, scope_sel = self.tier.get(), self.scope_var.get()
        nav, sidebar_visible, status_text = self.nav_selection, self.sidebar_visible, self.status.get()
        self.close_thinking_menu()
        self.shell.destroy()
        self._style()
        self._build()
        self._stream_active = False
        self._stream_buffer = ""
        self._token_pending = ""
        self.chat_log = []
        self.message_index = 0
        for role, text in chat_log:
            self.chat(role, text)
        self.console_log = []
        self.command_count = 0
        for payload in console_log:
            self._console_write(payload)
        self.command_badge.set(str(self.command_count))
        self.rows = {}
        for payload in rows:
            iid = self.tree.insert("", "end", values=(payload["kind"], self._display_path(payload["path"]),
                                                      payload["time"]), tags=(payload["kind"],))
            self.rows[iid] = payload
        if rows:
            self.files_empty.place_forget()
        self.file_count.set(str(len({item["path"] for item in rows})))
        if report:
            self.delivery_empty.place_forget()
            render_markdown(self.delivery, report, clear=True)
            self.copy_button.configure(state="normal")
            self.export_button.configure(state="normal")
        if welcome_visible:
            self.welcome.place(relx=0, rely=0, relwidth=1, relheight=1)
        else:
            self.welcome.place_forget()
        if input_text:
            self.input.insert("1.0", input_text)
            self._update_placeholder()
        self.task_title.configure(text=task_title)
        self.sidebar_task.set(sidebar_task)
        self.tier.set(tier_sel)
        self.scope_var.set(scope_sel)
        self._select_nav(nav)
        if not sidebar_visible:
            self.toggle_sidebar()
        self.status.set(status_text)
        self.refresh_model_ui()
        self._refresh_theme_buttons()
        self.refresh_conversations_ui()
        self._window_chrome()
        if self.quick_window is not None and self.quick_window.winfo_exists():
            self.quick_window.apply_theme()

    # ---- 模式 / 等级 / 样式 ---------------------------------------------------

    def set_work_mode(self, mode: str):
        if mode not in VALID_WORK_MODES:
            raise ValueError(f"Unsupported work_mode: {mode}")
        if self.state.work_mode == mode:
            return
        if self.agent.running or self.ui_busy:
            self.status.set("请先完成或停止当前任务，再切换模式")
            return
        self.close_thinking_menu()
        self.state.update(work_mode=mode)
        self.config = replace(self.config, work_mode=mode)
        self._persist_settings()
        if self.review:
            self.review.finish(False)
        self._refresh_mode_and_thinking_styles()
        self._position_panes()
        # Keep the model context and visible conversation consistent on mode changes.
        set_text(self.chat_text, "")
        self.message_index = 0
        self.chat_log = []
        for message in self.agent.history:
            self.chat("用户" if message["role"] == "user" else "ClawX", message["content"])
        if self.agent.history:
            self.welcome.place_forget()
        else:
            self.welcome.place(relx=0, rely=0, relwidth=1, relheight=1)
        mode_desc = (
            "已切换至「开发」：启用本地工具、四档安全审查、文件监控与终端执行。"
            if mode == "work"
            else "已切换至「聊天 · " + CHAT_STYLE_LABELS[self.config.chat_style] + "」：可以随时切换即时回答和深入讨论。"
        )
        self.chat("模式", mode_desc)
        self.status.set("当前模式：" + WORK_MODE_LABELS[mode])

    def toggle_work_mode(self):
        next_mode = "chat" if self.state.work_mode == "work" else "work"
        self.set_work_mode(next_mode)

    def set_thinking_level(self, level: str):
        if level not in VALID_THINKING_LEVELS:
            raise ValueError(f"Unsupported thinking_level: {level}")
        supported = self._active_levels()
        if level not in supported:
            level = nearest_level(level, supported)
        if self.agent.running or self.ui_busy:
            self.thinking_slider.set_level(self.state.thinking_level)
            self.status.set("请先完成或停止当前任务，再调整思考等级")
            return
        self.state.update(thinking_level=level)
        self.config = replace(self.config, thinking_level=level)
        self._persist_settings()
        self._refresh_mode_and_thinking_styles()
        self.status.set("设置已更新")

    def set_chat_style(self, style: str):
        if style not in CHAT_STYLE_LABELS:
            raise ValueError(f"Unsupported chat_style: {style}")
        if self.agent.running or self.ui_busy:
            self.status.set("请先完成或停止当前任务，再切换回答方式")
            return
        if style == self.config.chat_style:
            return
        self.close_thinking_menu()
        self.config = replace(self.config, chat_style=style)
        self._persist_settings()
        self._refresh_mode_and_thinking_styles()
        self.status.set("回答方式：" + CHAT_STYLE_LABELS[style])

    def toggle_ultra_thinking(self):
        supported = self._active_levels()
        if self.state.work_mode == "chat" and self.config.chat_style == "instant":
            candidates = [level for level in INSTANT_LEVELS if level in supported] or supported
        else:
            candidates = supported
        current = self.config.effective_level()
        highest = max(candidates, key=THINKING_LEVELS.index)
        fallback = nearest_level("Medium", candidates)
        target = fallback if current == highest else highest
        self.set_thinking_level(target)

    def new_task(self):
        if self.agent.running or self.ui_busy:
            self.status.set("任务执行中，请先完成或停止当前任务")
            return
        self._end_session()
        self.close_thinking_menu()
        # Preserve all generated files. Only clear the conversation session and views.
        for link in self.chat_text.tag_names():
            if link.startswith("request_"):
                self.chat_text.tag_delete(link)
        self.agent.history.clear()
        self.rows.clear()
        self.tree.delete(*self.tree.get_children())
        self.report = ""
        self.command_count = 0
        self.file_count.set("0")
        self.command_badge.set("0")
        self.task_title.configure(text="新任务")
        self.sidebar_task.set("还没有开始任务")
        set_text(self.chat_text, "")
        set_text(self.console, "等待执行命令…\n")
        set_text(self.delivery, "")
        self.chat_log = []
        self.console_log = []
        self.message_index = 0
        self.input.delete("1.0", "end")
        self._update_placeholder()
        self.files_empty.place(relx=0.5, rely=0.52, anchor="center")
        self.delivery_empty.place(relx=0.5, rely=0.5, anchor="center")
        self.delivery_hint.configure(text="任务完成后查看输出的文件夹与文件")
        self.copy_button.configure(state="disabled")
        self.export_button.configure(state="disabled")
        self.welcome.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.focus_panel("chat")
        self.status.set("就绪")

    def policy_changed(self, **changes):
        self.state.update(**changes)
        if self.review:
            self.review.finish(False)
        self.chat("安全", f"当前档位 {self.state.tier}，范围：{'整个计算机' if self.state.whole_computer else self.state.root}。旧授权已失效。")

    def change_tier(self, _event=None):
        tier = TIERS.index(self.tier.get()) + 1
        if tier == 4 and self.state.tier != 4:
            accepted = messagebox.askyesno(
                "启用全部允许：风险确认",
                "“全部允许”会跳过 AI 安全审查和逐项人工审批。\n\n"
                "Agent 可自动创建、覆盖、删除文件并执行当前范围允许的命令，"
                "错误指令或不可信内容可能导致数据丢失或信息泄露。\n\n"
                "指定目录模式仍受目录与受限命令约束；整个计算机模式允许跨目录操作，"
                "并可执行任意 Shell 命令，影响当前账户能够访问的资源。\n\n"
                "请确认工作范围和重要文件备份。仍要启用“全部允许”吗？",
                parent=self.root, default="no", icon="warning")
            if not accepted:
                self.tier.set(TIERS[self.state.tier-1])
                self.status.set("已取消启用全部允许，保留原安全档位")
                return
        self.policy_changed(tier=tier)
        self.safety_label.configure(text="● " + TIERS[tier - 1].split(" · ")[1], foreground=TIER_COLORS[tier])

    def change_scope(self, _event=None):
        whole = self.scope_var.get() == "整个计算机模式"
        if whole and not self.state.whole_computer:
            allowed = messagebox.askyesno("高危操作范围确认", "整个计算机模式将允许跨目录、跨盘符文件操作和任意 Shell 命令。\n"
                "档位 4 会直接执行，无额外审批。命令可访问当前账户能够访问的全部资源。\n\n确认解除目录边界？", parent=self.root, default="no", icon="warning")
            if not allowed:
                self.scope_var.set("指定目录模式")
                return
        self.policy_changed(whole_computer=whole)

    def choose_directory(self):
        directory = filedialog.askdirectory(parent=self.root, initialdir=self.state.root, mustexist=True)
        if directory:
            canonical = os.path.realpath(directory)
            project = self.projects.active()
            project["root"] = canonical
            try:
                self.projects.replace([project if item["id"] == project["id"] else item
                                       for item in self.projects.all()], project["id"])
            except (ValueError, OSError) as exc:
                messagebox.showerror("目录更新失败", str(exc), parent=self.root)
                return
            self.policy_changed(root=canonical)
            self.refresh_workspace_ui()

    def send(self):
        task = self.input.get("1.0", "end-1c").strip()
        if not task or self.agent.running or self.ui_busy:
            return
        if not self.config.model:
            messagebox.showerror("未配置模型", "请先在侧栏「模型与连接」中选择模型并填写模型 ID。", parent=self.root)
            return
        if not self.config.api_key:
            try:
                hostname = urllib.parse.urlsplit(self.config.base_url).hostname
            except ValueError:
                hostname = None
            if hostname not in {"localhost", "127.0.0.1", "::1"}:
                messagebox.showerror("未配置 API Key", "请先在侧栏「模型与连接」中填写 API Key。", parent=self.root)
                return
        if len(task) > 30000:
            messagebox.showerror("任务过长", "请将任务限制在 30000 字符内。", parent=self.root)
            return
        self.close_thinking_menu()
        self.chat("用户", task)
        self.welcome.place_forget()
        self.task_title.configure(text=task.splitlines()[0][:45])
        self.sidebar_task.set(task[:90])
        self.input.delete("1.0", "end")
        self._update_placeholder()
        self.rows.clear()
        self.tree.delete(*self.tree.get_children())
        self.report = ""
        self.command_count = 0
        set_text(self.console, "")
        self.command_badge.set("0")
        self.file_count.set("0")
        self.files_empty.place(relx=0.5, rely=0.52, anchor="center")
        self.delivery_empty.place(relx=0.5, rely=0.5, anchor="center")
        self.delivery_hint.configure(text="正在执行，任务结束后整理交付文件清单")
        self.copy_button.configure(state="disabled")
        self.export_button.configure(state="disabled")
        set_text(self.delivery, "")
        self.ui_busy = True
        self._task_public_replies = []
        self._lock_mode_controls(True)
        self.status.set("准备执行任务")
        self.send_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        if self.session_id is None:
            self.session_id = uuid.uuid4().hex
            self.session_created = self._now()
        try:
            self.agent.start(task, replace(self.config))
            self._append_session("user", task, self.state.work_mode)
        except PolicyError as exc:
            self.ui_busy = False
            self._lock_mode_controls(False)
            self.chat("错误", str(exc))
            self.send_button.configure(state="normal")
            self.stop_button.configure(state="disabled")
            self.status.set("无法启动任务")

    def stop(self):
        self.agent.stop()
        if self.review:
            self.review.finish(False)
        self.status.set("正在停止 · 取消等待模型响应并终止命令；已发送的 API 请求无法撤回")

    def pump(self):
        # Wake signals must not wait behind a long stream of model tokens.
        for _ in range(8):
            try:
                kind, payload = self.quick_events.get_nowait()
            except queue.Empty:
                break
            if kind == "quick_wake":
                self.show_quick_composer()
            elif kind == "hotkey_error":
                self.status.set("快速唤醒不可用：" + payload)
        # Reveal a bounded amount per frame. Even a gateway returning one large
        # delta stays readable, while all following events retain their order.
        reveal_pending = bool(self._token_pending)
        if reveal_pending:
            self._reveal_tokens()
        for _ in range(0 if reveal_pending else 160):
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "chat":
                self.chat(*payload)
            elif kind == "thinking":
                continue
            elif kind == "execution_start":
                if payload["mode"] == "work":
                    self._execution_started = payload["started"]
                    self.chat("执行时间", "执行中 · <1s")
            elif kind == "execution_end":
                if payload["mode"] == "work":
                    self._set_execution_time(payload["elapsed"], finished=True)
                self._execution_started = None
            elif kind == "token":
                self._token_pending = payload.get("text", "")
                self._reveal_tokens()
                if not self.agent.cancel.is_set() and payload.get("text"):
                    break
            elif kind == "stream_end":
                self._stream_finalize(payload.get("text"))
            elif kind == "connection_test":
                dialog, ok, text = payload
                if dialog.winfo_exists():
                    dialog.test_button.configure(state="normal")
                    dialog.test_status.set(text)
                    if not ok:
                        messagebox.showerror("连接测试失败", text, parent=dialog)
            elif kind == "console":
                self._console_write(payload)
            elif kind == "file":
                self.files_empty.place_forget()
                iid = self.tree.insert("", "end", values=(payload["kind"], self._display_path(payload["path"]), payload["time"]), tags=(payload["kind"],))
                self.rows[iid] = payload
                self.file_count.set(str(len({item["path"] for item in self.rows.values()})))
                self.tree.see(iid)
            elif kind == "status":
                self.status.set(payload)
            elif kind == "deliverable":
                self.report = payload
                self.delivery_empty.place_forget()
                render_markdown(self.delivery, payload, clear=True)
                self.copy_button.configure(state="normal")
                self.export_button.configure(state="normal")
            elif kind == "approval":
                if self.agent.cancel.is_set() or self.state.revision != payload.revision or self.closing:
                    payload.answer = (False, "")
                    payload.done.set()
                else:
                    self.status.set("等待用户审批")
                    self.review = ReviewDialog(self, payload)
            elif kind == "done":
                self._stream_finalize()
                self.ui_busy = False
                self._lock_mode_controls(False)
                if self.review:
                    self.review.finish(False)
                self.send_button.configure(state="normal")
                self.stop_button.configure(state="disabled")
                self._record_done()
                self.status.set("任务" + payload)
        if self._execution_started is not None:
            self._set_execution_time(time.monotonic() - self._execution_started)
        if not self.closing:
            self.root.after(16 if self._stream_active or self._token_pending else 40, self.pump)

    def _reveal_tokens(self):
        remaining = len(self._token_pending)
        count = remaining if self.agent.cancel.is_set() else max(2, min(256, remaining // 24))
        self._stream_token(self._token_pending[:count])
        self._token_pending = self._token_pending[count:]

    def _console_write(self, payload):
        if "[RUNNING]" in payload:
            self.command_count += 1
            self.command_badge.set(str(self.command_count))
        tag = "error" if "[BLOCK]" in payload or "[stderr]" in payload else "success" if "[EXIT 0]" in payload else "command" if payload.startswith("\n$") else "meta"
        self.console.configure(state="normal")
        self.console.insert("end", payload, tag)
        if int(self.console.index("end-1c").split(".")[0]) > 6000:
            self.console.delete("1.0", "1500.0")
        self.console.configure(state="disabled")
        self.console.see("end")
        self.console_log.append(payload)
        if len(self.console_log) > 5000:
            del self.console_log[:2000]

    def _stream_token(self, text):
        """逐字渲染模型输出：首个 token 建立气泡，后续追加，结束统一重排为 Markdown。"""
        if not text:
            return
        if not self._stream_active:
            self._stream_active = True
            self._stream_buffer = ""
            self.message_index += 1
            self.chat_text.configure(state="normal")
            self.chat_text.insert("end", "✳  ClawX\n", "heading")
            self._stream_start_index = self.chat_text.index("end-1c")
        else:
            self.chat_text.configure(state="normal")
        self._stream_buffer += text
        self.chat_text.insert("end", text, "assistant")
        self.chat_text.see("end")
        self.chat_text.configure(state="disabled")

    def _stream_finalize(self, final_text=None):
        if not self._stream_active:
            return
        self._stream_active = False
        if final_text is not None:
            self._stream_buffer = final_text
        self.chat_text.configure(state="normal")
        try:
            self.chat_text.delete(self._stream_start_index, "end-1c")
        except tk.TclError:
            pass
        render_markdown(self.chat_text, self._stream_buffer, base="assistant")
        self.chat_text.insert("end", "\n")
        self.chat_text.configure(state="disabled")
        self.chat_text.see("end")
        self.chat_log.append(("ClawX", self._stream_buffer))
        if self.ui_busy:
            self._task_public_replies.append(self._stream_buffer)
        if len(self.chat_log) > 4000:
            del self.chat_log[:1500]
        self._stream_buffer = ""

    def preview_file(self, _event=None):
        selection = self.tree.selection()
        if not selection:
            return
        item = self.rows[selection[0]]
        self._show_text(item["kind"] + " · " + item["path"], item["preview"], diff=True)

    def _show_text(self, title, content, *, diff=False):
        window = tk.Toplevel(self.root)
        window.title("ClawX · " + title)
        window.geometry("960x640")
        window.configure(bg=theme.bg)
        window.transient(self.root)
        ttk.Label(window, text=title, wraplength=900).pack(anchor="w", padx=18, pady=16)
        text = text_box(window, wrap="none")
        text.tag_configure("add", foreground=theme.green, background=theme.diff_add_bg)
        text.tag_configure("remove", foreground=theme.red, background=theme.diff_remove_bg)
        text.tag_configure("hunk", foreground=theme.diff_hunk_fg)
        text.configure(state="normal")
        for line in content.splitlines(True):
            tag = "add" if diff and line.startswith("+") else "remove" if diff and line.startswith("-") else "hunk" if diff and line.startswith("@@") else ""
            text.insert("end", line, tag)
        text.configure(state="disabled")

    def copy_report(self):
        if self.report:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.report)
            self.status.set("交付文件清单已复制")

    def export_report(self):
        if not self.report:
            return
        path = filedialog.asksaveasfilename(parent=self.root, initialfile="ClawX-report.md", defaultextension=".md",
                                           filetypes=[("Markdown", "*.md"), ("Text", "*.txt")])
        if path:
            try:
                Path(path).write_text(self.report, encoding="utf-8")
                self.status.set("交付文件清单已导出：" + path)
            except OSError as exc:
                messagebox.showerror("导出失败", str(exc), parent=self.root)

    def close(self):
        self.closing = True
        self.wake_hotkey.close()
        if self.quick_window is not None and self.quick_window.winfo_exists():
            self.quick_window.destroy()
            self.quick_window = None
        self._end_session()
        self.stop()
        self._wait_close()

    def _wait_close(self):
        # Keep the event loop alive until the worker has cleaned up child processes.
        if self.agent.running:
            self.status.set("正在关闭，等待后台请求或进程清理完成…")
            self.root.after(100, self._wait_close)
        else:
            # Widgets own their Tcl callbacks; cancel them before global shutdown.
            self.close_thinking_menu()
            try:
                for identifier in self.root.tk.call("after", "info"):
                    self.root.after_cancel(identifier)
            except tk.TclError:
                pass
            self.root.destroy()


def main():
    root = tk.Tk()
    ClawXApp(root)
    root.mainloop()
