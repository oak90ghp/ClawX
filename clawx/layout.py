"""Modern chat/development dashboard and five-stop reasoning slider."""
from pathlib import Path
import tkinter as tk
from tkinter import ttk

from .core import INSTANT_LABELS, INSTANT_LEVELS, THINKING_LEVELS, nearest_level
from .widgets import (
    theme, UI_FONT, CODE_FONT, TIERS, TIER_COLORS,
    WORK_MODE_LABELS, WORK_MODE_COLORS, CHAT_STYLE_LABELS,
    RoundedPanel, ThinkingSlider, ThinkingMenuButton,
    text_box, set_text,
)


def label(parent, text="", *, bg=None, fg=None, size=10, bold=False, **kwargs):
    return tk.Label(parent, text=text, bg=bg or theme.bg, fg=fg or theme.fg, borderwidth=0,
                    font=(UI_FONT, size, "bold" if bold else "normal"), **kwargs)


def flat_button(parent, text, command, *, bg=None, fg=None, size=10, padx=10, pady=7):
    background = bg or theme.bg
    button = tk.Button(parent, text=text, command=command, bg=background, fg=fg or theme.muted,
                       activebackground=theme.hover, activeforeground=theme.fg, relief="flat",
                       borderwidth=0, highlightthickness=0, cursor="hand2",
                       font=(UI_FONT, size), padx=padx, pady=pady, anchor="w")
    button.bind("<Enter>", lambda _e: button.configure(bg=theme.hover))
    button.bind("<Leave>", lambda _e: button.configure(bg=background))
    return button


class DashboardLayout:
    def _style(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", background=theme.bg, foreground=theme.fg, font=(UI_FONT, 10))
        style.configure("TFrame", background=theme.bg)
        style.configure("TLabel", background=theme.bg, foreground=theme.fg)
        style.configure("Title.TLabel", font=(UI_FONT, 17, "bold"), foreground=theme.fg)
        style.configure("Muted.TLabel", foreground=theme.muted)
        style.configure("TButton", padding=(14, 7), background=theme.panel, foreground=theme.fg,
                        borderwidth=0, focusthickness=0, focuscolor=theme.panel)
        style.map("TButton", background=[("active", theme.hover), ("disabled", theme.field)],
                  foreground=[("disabled", theme.disabled_fg)])
        style.configure("Primary.TButton", background=theme.accent, foreground=theme.primary_fg,
                        font=(UI_FONT, 10, "bold"), padding=(17, 7))
        style.map("Primary.TButton", background=[("active", theme.primary_active), ("disabled", theme.primary_disabled_bg)],
                  foreground=[("disabled", theme.primary_disabled_fg)])
        style.configure("Ghost.TButton", background=theme.bg, foreground=theme.muted, padding=(9, 5))
        style.map("Ghost.TButton", background=[("active", theme.panel)], foreground=[("active", theme.fg)])
        style.configure("Danger.TButton", foreground=theme.red)
        style.configure("TEntry", fieldbackground=theme.field, foreground=theme.fg, insertcolor=theme.fg,
                        bordercolor=theme.border, lightcolor=theme.border, darkcolor=theme.border, padding=7)
        style.configure("TCombobox", fieldbackground=theme.field, foreground=theme.fg, arrowsize=11,
                        bordercolor=theme.border, lightcolor=theme.border, darkcolor=theme.border, padding=5)
        style.map("TCombobox", fieldbackground=[("readonly", theme.field)],
                  foreground=[("readonly", theme.fg)], selectbackground=[("readonly", theme.field)],
                  selectforeground=[("readonly", theme.fg)])
        style.configure("TCheckbutton", background=theme.bg, padding=4)
        style.map("TCheckbutton", background=[("active", theme.bg)])
        style.configure("TLabelframe", background=theme.bg, bordercolor=theme.border)
        style.configure("TLabelframe.Label", background=theme.bg, foreground=theme.muted, font=(UI_FONT, 10))
        style.configure("Treeview", background=theme.bg, fieldbackground=theme.bg, foreground=theme.fg,
                        rowheight=32, borderwidth=0, lightcolor=theme.bg, darkcolor=theme.bg,
                        bordercolor=theme.bg, font=(UI_FONT, 9))
        style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        style.configure("Treeview.Heading", background=theme.bg, foreground=theme.muted,
                        relief="flat", lightcolor=theme.bg, darkcolor=theme.bg, bordercolor=theme.bg,
                        font=(UI_FONT, 9), padding=(6, 9))
        style.map("Treeview", background=[("selected", theme.select_bg)], foreground=[("selected", theme.fg)])
        style.map("Treeview.Heading", background=[("active", theme.field)])
        style.configure("TPanedwindow", background=theme.border, sashwidth=1, sashpad=0)
        style.configure("Sash", sashthickness=2, gripcount=0, background=theme.border)
        style.layout("Horizontal.Sash", [("Sash.hsash", {"sticky": "nswe"})])
        style.layout("Vertical.Sash", [("Sash.vsash", {"sticky": "nswe"})])
        style.configure("Vertical.TScrollbar", background=theme.scrollbar, troughcolor=theme.bg,
                        bordercolor=theme.bg, lightcolor=theme.scrollbar, darkcolor=theme.scrollbar,
                        borderwidth=0, arrowsize=7, gripcount=0)
        style.map("Vertical.TScrollbar", background=[("active", theme.scrollbar_active), ("disabled", theme.bg)],
                  lightcolor=[("disabled", theme.bg)], darkcolor=[("disabled", theme.bg)])
        style.layout("Vertical.TScrollbar", [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
            ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
        style.configure("Horizontal.TScrollbar", background=theme.scrollbar, troughcolor=theme.bg,
                        bordercolor=theme.bg, lightcolor=theme.scrollbar, darkcolor=theme.scrollbar,
                        borderwidth=0, arrowsize=7)
        self.root.option_add("*TCombobox*Listbox.background", theme.field)
        self.root.option_add("*TCombobox*Listbox.foreground", theme.fg)
        self.root.option_add("*TCombobox*Listbox.selectBackground", theme.select)

    def _build(self):
        self.sidebar_visible = True
        self.sidebar_width = 200
        self.command_count = 0
        self.ui_busy = False
        self.nav_buttons = {}
        self.mode_buttons = {}
        self.chat_style_buttons = {}
        self.thinking_menu_open = False
        self.nav_selection = "chat"
        self.status = tk.StringVar(value="就绪")
        self.status.trace_add("write", self._status_changed)
        self.shell = tk.Frame(self.root, bg=theme.bg)
        self.shell.pack(fill="both", expand=True, padx=6, pady=6)
        self.sidebar = tk.Frame(self.shell, bg=theme.sidebar, width=self.sidebar_width)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        self.sidebar_rule = tk.Frame(self.shell, bg=theme.border, width=1)
        self.sidebar_rule.pack(side="left", fill="y")
        self.main_area = tk.Frame(self.shell, bg=theme.bg)
        self.main_area.pack(side="left", fill="both", expand=True)
        self._build_sidebar()
        self._build_header()
        self._build_controls()
        self._build_workspace()
        self._build_footer()
        self.root.bind("<Button-1>", self._dismiss_thinking_menu)
        self.root.bind("<Configure>", self._thinking_window_resize)
        self.root.bind("<Unmap>", self._thinking_window_resize)
        self.root.bind("<Control-n>", lambda _e: self.new_task())
        self.root.bind("<Control-b>", lambda _e: self.toggle_sidebar())
        self.root.bind("<Control-m>", lambda _e: self.toggle_work_mode())
        self.root.bind("<Control-u>", lambda _e: self.toggle_ultra_thinking())
        self.root.bind("<Escape>", self._escape)
        self.root.after(180, self._position_panes)

    def _build_sidebar(self):
        brand = tk.Frame(self.sidebar, bg=theme.sidebar)
        brand.pack(fill="x", padx=18, pady=(22, 22))
        icon = tk.Canvas(brand, width=30, height=30, bg=theme.sidebar, highlightthickness=0)
        icon.pack(side="left", padx=(0, 10))
        for x, y, x2, y2 in ((5, 7, 13, 15), (17, 15, 25, 23), (5, 23, 13, 15), (17, 15, 25, 7)):
            icon.create_line(x, y, x2, y2, fill=theme.accent, width=4, capstyle="round")
        label(brand, "ClawX", bg=theme.sidebar, size=17, bold=True).pack(side="left")
        label(brand, "LOCAL", bg=theme.sidebar, fg=theme.muted, size=8).pack(side="right", pady=(5, 0))
        new = flat_button(self.sidebar, "+   新建任务", self.new_task, bg=theme.panel, fg=theme.fg, pady=10, padx=14)
        new.pack(fill="x", padx=14)
        label(self.sidebar, "工作区", bg=theme.sidebar, fg=theme.section, size=9, anchor="w").pack(fill="x", padx=22, pady=(24, 8))
        self.workspace_name = label(self.sidebar, self.state.project_name,
                                    bg=theme.sidebar, fg=theme.fg, anchor="w", size=10, bold=True)
        self.workspace_name.pack(fill="x", padx=22, pady=(0, 2))
        self.workspace_path = label(self.sidebar, Path(self.state.root).name or self.state.root,
                                    bg=theme.sidebar, fg=theme.muted, anchor="w", size=8)
        self.workspace_path.pack(fill="x", padx=22, pady=(0, 10))
        for key, title in (("chat", "对话"), ("files", "变更文件"), ("delivery", "交付报告")):
            row = tk.Frame(self.sidebar, bg=theme.sidebar)
            row.pack(fill="x", padx=12, pady=2)
            button = flat_button(row, {"chat": "◉", "files": "▤", "terminal": ">_", "delivery": "▧"}[key] + "    " + title,
                                 lambda k=key: self.focus_panel(k), bg=theme.sidebar, fg=theme.muted, padx=13, pady=9)
            button.pack(fill="x")
            self.nav_buttons[key] = button
        self._select_nav("chat")
        # 底部管理区先占位，保证「对话记录」列表在空间不足时自行收缩。
        bottom = tk.Frame(self.sidebar, bg=theme.sidebar)
        bottom.pack(side="bottom", fill="x", padx=12, pady=14)
        tk.Frame(bottom, bg=theme.border, height=1).pack(fill="x", pady=(0, 12))
        flat_button(bottom, "◇    Skill 管理", self._open_skills, bg=theme.sidebar).pack(fill="x")
        flat_button(bottom, "▱    项目设置", self._open_projects, bg=theme.sidebar).pack(fill="x")
        flat_button(bottom, "⚙    模型与连接", self._open_config, bg=theme.sidebar).pack(fill="x")
        appearance = tk.Frame(bottom, bg=theme.sidebar)
        appearance.pack(fill="x", pady=(10, 0))
        label(appearance, "外观", bg=theme.sidebar, fg=theme.section, size=9, anchor="w").pack(fill="x", padx=12)
        row = tk.Frame(appearance, bg=theme.sidebar)
        row.pack(fill="x", padx=6, pady=(3, 0))
        self.theme_buttons = {}
        for key, title in (("light", "浅色"), ("dark", "深色"), ("system", "系统")):
            button = flat_button(row, title, lambda k=key: self.set_theme(k),
                                 bg=theme.sidebar, size=8, padx=4, pady=3)
            button.pack(side="left", fill="x", expand=True, padx=1)
            self.theme_buttons[key] = button
        self._refresh_theme_buttons()
        self.connection_label = label(bottom, "●  已配置 API" if self.config.api_key else "●  未配置 API Key",
                                      bg=theme.sidebar, fg=theme.green if self.config.api_key else theme.waiting,
                                      size=8, anchor="w")
        self.connection_label.pack(fill="x", padx=12, pady=(16, 0))
        # 项目切换 + 当前任务
        self.sidebar_task = tk.StringVar(value="还没有开始任务")
        label(self.sidebar, "当前任务", bg=theme.sidebar, fg=theme.section, size=9, anchor="w").pack(fill="x", padx=22, pady=(14, 6))
        tk.Label(self.sidebar, textvariable=self.sidebar_task, bg=theme.sidebar, fg=theme.muted,
                 font=(UI_FONT, 9), wraplength=150, justify="left", anchor="nw").pack(fill="x", padx=22)
        label(self.sidebar, "项目", bg=theme.sidebar, fg=theme.section, size=9, anchor="w").pack(fill="x", padx=22, pady=(12, 3))
        self.project_var = tk.StringVar(value=self.state.project_name)
        self.project_combo = ttk.Combobox(self.sidebar, textvariable=self.project_var, state="readonly")
        self.project_combo.pack(fill="x", padx=14)
        self.project_combo.bind("<<ComboboxSelected>>", self._sidebar_project_changed)
        # 对话记录列表（点击加载，右侧小按钮删除所选）
        record_row = tk.Frame(self.sidebar, bg=theme.sidebar)
        record_row.pack(fill="x", padx=22, pady=(12, 3))
        label(record_row, "对话记录", bg=theme.sidebar, fg=theme.section, size=9, anchor="w").pack(side="left")
        flat_button(record_row, "删除", self._conversation_deleted, bg=theme.sidebar,
                    size=8, padx=4, pady=1).pack(side="right")
        self.conv_listbox = tk.Listbox(self.sidebar, bg=theme.field, fg=theme.fg, selectbackground=theme.select,
                                       relief="flat", highlightthickness=0, font=(UI_FONT, 9),
                                       exportselection=False)
        self.conv_listbox.pack(fill="both", expand=True, padx=14, pady=(0, 8))
        self.conv_listbox.bind("<<ListboxSelect>>", self._conversation_selected)
        self.conv_listbox.bind("<Button-3>", self._conversation_context_menu)
        self.conv_context_menu = tk.Menu(self.conv_listbox, tearoff=False, bg=theme.field, fg=theme.fg,
                                         activebackground=theme.select, activeforeground=theme.fg)
        self.conv_context_menu.add_command(label="创建分支对话…")
        self.conv_map = {}

    def _refresh_theme_buttons(self):
        mode = getattr(self.config, "theme", "dark")
        for key, button in getattr(self, "theme_buttons", {}).items():
            active = mode == key
            background = theme.nav_active_bg if active else theme.sidebar
            button.configure(fg=theme.accent if active else theme.muted, bg=background)
            button.bind("<Leave>", lambda _e, item=button, color=background: item.configure(bg=color))

    def _build_header(self):
        header = tk.Frame(self.main_area, bg=theme.bg, height=59)
        header.pack(fill="x")
        header.pack_propagate(False)
        flat_button(header, "☰", self.toggle_sidebar, size=14, padx=13).pack(side="left", padx=(9, 3), pady=9)
        label(header, "工作台", fg=theme.muted, size=10).pack(side="left")
        label(header, "/", fg=theme.slash).pack(side="left", padx=13)
        self.task_title = label(header, "新任务", size=10, bold=True, anchor="w", width=1)
        self.task_title.pack(side="left", fill="x", expand=True)
        self.mode_label = label(header, self.config.model or "未配置模型",
                                bg=theme.panel, fg=theme.accent, size=9, padx=10, pady=4)
        self.mode_label.pack(side="right", padx=(8, 18))
        self.work_mode_badge = label(header, WORK_MODE_LABELS.get(self.state.work_mode, WORK_MODE_LABELS["work"]),
                                     bg=theme.panel, fg=WORK_MODE_COLORS.get(self.state.work_mode, theme.accent),
                                     size=9, padx=10, pady=4)
        self.work_mode_badge.pack(side="right", padx=(4, 4))
        self.activity_label = label(header, "●  就绪", fg=theme.green, size=9)
        self.activity_label.pack(side="right", padx=8)
        tk.Frame(self.main_area, bg=theme.border, height=1).pack(fill="x")

    def _build_controls(self):
        controls = tk.Frame(self.main_area, bg=theme.bg)
        controls.pack(fill="x", padx=18, pady=(10, 9))
        mode_row = tk.Frame(controls, bg=theme.bg)
        mode_row.pack(fill="x", pady=(0, 9))
        mode_switch = tk.Frame(mode_row, bg=theme.field, highlightbackground=theme.border, highlightthickness=1)
        mode_switch.pack(side="left")
        for mode_key, title in (("chat", "聊天"), ("work", "开发")):
            button = tk.Button(mode_switch, text=title,
                               command=lambda m=mode_key: self.set_work_mode(m),
                               relief="flat", borderwidth=0, highlightthickness=0,
                               cursor="hand2", font=(UI_FONT, 9, "bold"), padx=15, pady=5)
            button.pack(side="left")
            self.mode_buttons[mode_key] = button
        self.chat_style_switch = tk.Frame(mode_row, bg=theme.field, highlightbackground=theme.border, highlightthickness=1)
        for key, title in CHAT_STYLE_LABELS.items():
            button = tk.Button(self.chat_style_switch, text=title,
                               command=lambda style=key: self.set_chat_style(style),
                               relief="flat", borderwidth=0, highlightthickness=0,
                               cursor="hand2", font=(UI_FONT, 9), padx=13, pady=5)
            button.pack(side="left")
            self.chat_style_buttons[key] = button
        scope_row = self.scope_row = tk.Frame(controls, bg=theme.bg)
        scope_row.pack(fill="x")
        self.tier = tk.StringVar(value=TIERS[1])
        tier = ttk.Combobox(scope_row, values=TIERS, textvariable=self.tier, state="readonly", width=21)
        tier.pack(side="right")
        tier.bind("<<ComboboxSelected>>", self.change_tier)
        self.scope_var = tk.StringVar(value="指定目录模式")
        scope = ttk.Combobox(scope_row, values=["指定目录模式", "整个计算机模式"],
                             textvariable=self.scope_var, state="readonly", width=14)
        scope.pack(side="right", padx=(8, 10))
        scope.bind("<<ComboboxSelected>>", self.change_scope)
        flat_button(scope_row, "▱", self.choose_directory, fg=theme.accent, padx=6, pady=3).pack(side="left")
        self.path_label = label(scope_row, self.state.root, fg=theme.muted, size=9, anchor="w", width=1)
        self.path_label.pack(side="left", fill="x", expand=True)

    def _lock_mode_controls(self, locked):
        state = "disabled" if locked else "normal"
        for button in (*self.mode_buttons.values(), *self.chat_style_buttons.values()):
            button.configure(state=state)
        self.thinking_slider.set_enabled(not locked)
        self.thinking_menu_button.set_enabled(not locked)
        if locked:
            self.close_thinking_menu()
        if self.quick_window is not None and self.quick_window.winfo_exists():
            self.quick_window.set_busy(locked)

    def _panel(self, parent, title, subtitle="", *, badge=None, action=None):
        panel = tk.Frame(parent, bg=theme.bg)
        header = tk.Frame(panel, bg=theme.bg, height=44)
        header.pack(fill="x", padx=15)
        header.pack_propagate(False)
        label(header, title, size=10, bold=True).pack(side="left")
        if badge is not None:
            tk.Label(header, textvariable=badge, bg=theme.panel, fg=theme.muted,
                     font=(UI_FONT, 8), padx=6, pady=1).pack(side="left", padx=8)
        if subtitle:
            label(header, subtitle, fg=theme.muted, size=8).pack(side="right")
        if action:
            flat_button(header, action[0], action[1], size=9, padx=7, pady=3).pack(side="right")
        tk.Frame(panel, bg=theme.border, height=1).pack(fill="x")
        return panel

    def _build_workspace(self):
        self.workspace_panes = ttk.Panedwindow(self.main_area, orient="horizontal")
        self.workspace_panes.pack(fill="both", expand=True, padx=(7, 14))
        self.center_panes = ttk.Panedwindow(self.workspace_panes, orient="vertical")
        self.inspector_panes = ttk.Panedwindow(self.workspace_panes, orient="vertical")
        self.workspace_panes.add(self.center_panes, weight=3)
        self.workspace_panes.add(self.inspector_panes, weight=2)
        self.file_count, self.command_badge = tk.StringVar(value="0"), tk.StringVar(value="0")
        chatframe = self._panel(self.center_panes, "对话", action=("分支对话", self._open_branch_dialog))
        terminalframe = self._panel(self.center_panes, "终端", badge=self.command_badge,
                                    action=("清空", self.clear_console))
        self.terminal_panel = terminalframe
        filesframe = self._panel(self.inspector_panes, "文件变更", badge=self.file_count)
        deliveryframe = self._panel(self.inspector_panes, "交付报告")
        self.center_panes.add(chatframe, weight=4)
        # Retain the unmanaged console for command logs and theme restoration.
        # Only the conversation is displayed in the central workspace.
        self.inspector_panes.add(filesframe, weight=1)
        self.inspector_panes.add(deliveryframe, weight=1)
        self._build_conversation(chatframe)
        self._build_files(filesframe)
        self.console = text_box(terminalframe, height=5, background=theme.console_bg,
                                font=(CODE_FONT, 9), pady=9)
        for tag, color in (("command", theme.fg), ("error", theme.red), ("success", theme.green), ("meta", theme.muted)):
            self.console.tag_configure(tag, foreground=color)
        set_text(self.console, "等待执行命令…\n")
        self._build_delivery(deliveryframe)

    def _build_conversation(self, parent):
        composer_area = tk.Frame(parent, bg=theme.bg)
        composer_area.pack(side="bottom", fill="x", padx=20, pady=(8, 14))

        self.composer = RoundedPanel(composer_area, height=154)
        self.composer.pack(fill="x")
        editor = tk.Frame(self.composer.body, bg=theme.panel)
        editor.pack(fill="both", expand=True)
        self.input = tk.Text(editor, height=3, bg=theme.panel, fg=theme.fg, insertbackground=theme.fg,
                             relief="flat", borderwidth=0, highlightthickness=0, undo=True,
                             font=(UI_FONT, 11), padx=2, pady=2, wrap="word")
        self.input.pack(fill="both", expand=True)
        self.input.bind("<Return>", self._keyboard_send)
        self.input.bind("<Shift-Return>", self._keyboard_newline)
        self.input.bind("<Control-Return>", self._keyboard_send)
        self.input.bind("<KeyRelease>", self._update_placeholder)
        self.input.bind("<FocusIn>", lambda _e: self.composer.focus_border(True))
        self.input.bind("<FocusOut>", lambda _e: self.composer.focus_border(False))
        self.placeholder = label(editor, "描述你的开发任务…", bg=theme.panel, fg=theme.placeholder, size=10, anchor="nw")
        self.placeholder.place(x=3, y=4)
        self.placeholder.bind("<Button-1>", lambda _e: self.input.focus_set())
        toolbar = tk.Frame(self.composer.body, bg=theme.panel)
        toolbar.pack(fill="x", pady=(5, 0))
        options = tk.Frame(self.composer.body, bg=theme.panel)
        options.pack(before=toolbar, fill="x", pady=(4, 0))
        self.safety_label = label(options, "● 逐项审批", bg=theme.panel, fg=TIER_COLORS[2], size=8)
        self.safety_label.pack(side="left", padx=(0, 8))

        # 二级菜单：模型选择 + 思考等级滑块
        self.thinking_controls = tk.Frame(self.main_area, bg=theme.panel,
                                          highlightbackground=theme.border, highlightthickness=1,
                                          padx=12, pady=10)
        self.thinking_caption = label(self.thinking_controls, "思考等级", bg=theme.panel, fg=theme.muted, size=8, anchor="w")
        self.thinking_caption.pack(fill="x")
        flat_button(self.thinking_controls, "收起 ▴", self.close_thinking_menu,
                    bg=theme.panel, size=8, pady=2).pack(anchor="e")
        label(self.thinking_controls, "模型", bg=theme.panel, fg=theme.muted, size=8, anchor="w").pack(fill="x", pady=(4, 2))
        self.model_var = tk.StringVar(value="")
        self.model_combo = ttk.Combobox(self.thinking_controls, textvariable=self.model_var, state="readonly")
        self.model_combo.pack(fill="x", pady=(0, 4))
        self.model_combo.bind("<<ComboboxSelected>>", self._model_combo_changed)
        self.thinking_slider = ThinkingSlider(self.thinking_controls, level=self.state.thinking_level,
                                              command=self.set_thinking_level)
        self.thinking_slider.pack(fill="x")
        self.thinking_menu_button = ThinkingMenuButton(toolbar, self.toggle_thinking_menu)
        self.thinking_menu_button.pack(side="left")
        self.answer_hint = label(toolbar, "", bg=theme.panel, fg=theme.muted, size=8)
        self.answer_hint.pack(side="left", padx=5)

        self.send_button = ttk.Button(toolbar, text="发送  ↑", style="Primary.TButton", command=self.send)
        self.send_button.pack(side="right")
        self.stop_button = ttk.Button(toolbar, text="停止", style="Danger.TButton", command=self.stop, state="disabled")
        self.stop_button.pack(side="right", padx=7)
        label(
            composer_area,
            "Enter 发送  ·  Shift+Enter 换行  ·  Esc 停止",
            fg=theme.hint,
            size=8, wraplength=450,
        ).pack(pady=(7, 0))
        self.conversation_host = tk.Frame(parent, bg=theme.bg)
        self.conversation_host.pack(fill="both", expand=True)
        self.chat_text = text_box(self.conversation_host, height=9, background=theme.bg,
                                  font=(UI_FONT, 10), padx=24, pady=18)
        self.chat_text.tag_configure("user", foreground=theme.fg, background=theme.user_bubble, lmargin1=12,
                                     lmargin2=12, rmargin=14, spacing1=10, spacing3=12)
        self.chat_text.tag_configure("assistant", foreground=theme.fg, spacing3=12)
        self.chat_text.tag_configure("meta", foreground=theme.muted, font=(UI_FONT, 9), spacing3=8)
        self.chat_text.tag_configure("error", foreground=theme.red, spacing3=10)
        self.chat_text.tag_configure("heading", font=(UI_FONT, 9, "bold"), foreground=theme.accent, spacing1=13, spacing3=6)
        self.chat_text.tag_configure("tool", foreground=theme.muted, font=(UI_FONT, 9), spacing3=3,
                                     lmargin1=20, lmargin2=20)
        self.chat_text.tag_configure("execution_heading", foreground=theme.fg, background=theme.panel,
                                     font=(UI_FONT, 9, "bold"), lmargin1=12, lmargin2=12,
                                     spacing1=8, spacing3=10)
        self.chat_text.tag_configure("execution_rule", foreground=theme.border,
                                     font=(CODE_FONT, 9), spacing1=12, spacing3=4)
        self.welcome = tk.Frame(self.conversation_host, bg=theme.bg)
        self.welcome.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._build_welcome()
        self._refresh_mode_and_thinking_styles()

    def _build_welcome(self):
        self.welcome.columnconfigure(0, weight=1)
        self.welcome.rowconfigure(0, weight=1)
        self.welcome.rowconfigure(2, weight=1)
        content = tk.Frame(self.welcome, bg=theme.bg)
        content.grid(row=1, column=0, sticky="ew", padx=32, pady=10)
        self.welcome_mark = label(content, "✳", fg=theme.accent, size=31)
        self.welcome_mark.pack(anchor="w", pady=(0, 10))
        self.welcome_title = label(content, "今天想构建什么？", size=24, bold=True, anchor="w")
        self.welcome_title.pack(fill="x")
        self.welcome_subtitle = label(
            content,
            "从聊天开始，或进入开发工作台。",
            fg=theme.muted,
            size=10,
            anchor="w",
            wraplength=520,
            justify="left",
        )
        self.welcome_subtitle.pack(fill="x", pady=(10, 24))
        cards = self.welcome_cards = tk.Frame(content, bg=theme.bg)
        cards.pack(fill="x")
        cards.columnconfigure((0, 1), weight=1, uniform="suggestions")
        self.suggestion_buttons = []
        self.suggestion_parts = []
        suggestions = [
            ("⌘", "构建一个功能", "把想法变成可运行的代码", "请帮我实现一个功能："),
            ("⌕", "检查与修复", "定位问题，整理修改方案", "请检查当前项目并提出修复建议。"),
            (">_", "规划项目结构", "创建目录与初始文件", "请帮我规划并创建当前项目的文件结构。"),
            ("◇", "阅读并总结", "浏览代码并给出总结", "请阅读当前项目并总结结构与主要功能。"),
        ]
        for i, (icon, title, description, prompt) in enumerate(suggestions):
            card = tk.Frame(cards, bg=theme.panel, highlightbackground=theme.border, highlightthickness=1, cursor="hand2")
            card.grid(row=i // 2, column=i % 2, sticky="nsew", padx=(0, 9) if i % 2 == 0 else (0, 0), pady=(0, 9))
            top = tk.Frame(card, bg=theme.panel)
            top.pack(fill="x", padx=13, pady=(12, 5))
            mark = label(top, icon, bg=theme.panel, fg=theme.accent, size=11)
            mark.pack(side="left", padx=(0, 8))
            heading = label(top, title, bg=theme.panel, size=10, anchor="w")
            heading.pack(side="left")
            desc = label(card, description, bg=theme.panel, fg=theme.muted, size=8, anchor="w", wraplength=200)
            desc.pack(fill="x", padx=13, pady=(0, 12))
            for widget in (card, top, mark, heading, desc):
                widget.bind("<Button-1>", lambda _e, value=prompt: self.prefill(value))
                widget.bind("<Enter>", lambda _e, box=card: box.configure(highlightbackground=theme.card_hover_border))
                widget.bind("<Leave>", lambda _e, box=card: box.configure(highlightbackground=theme.border))
            self.suggestion_buttons.append(card)
            self.suggestion_parts.append((card, top, mark, heading, desc))
        self.welcome.bind("<Configure>", self._welcome_resize)

    def _welcome_resize(self, event):
        compact, tiny = event.height < 310, event.height < 160
        self.welcome_title.configure(font=(UI_FONT, 17 if compact else 24, "bold"))
        self.welcome_subtitle.configure(wraplength=max(150, event.width - 70))
        if compact:
            self.welcome_mark.pack_forget()
            self.welcome_subtitle.pack_forget()
        else:
            self.welcome_mark.pack(before=self.welcome_title, anchor="w", pady=(0, 10))
            self.welcome_subtitle.pack(after=self.welcome_title, fill="x", pady=(10, 24))
        for index, card in enumerate(self.suggestion_buttons):
            if tiny or (compact and index < 2):
                card.grid_remove()
            else:
                card.grid()

    def _build_files(self, parent):
        host = tk.Frame(parent, bg=theme.bg)
        host.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(host, columns=("kind", "path", "time"), show="headings", selectmode="browse")
        for key, title, width, stretch in (("kind", "状态", 63, False), ("path", "文件", 190, True), ("time", "时间", 68, False)):
            self.tree.heading(key, text=title, anchor="w")
            self.tree.column(key, width=width, minwidth=45, stretch=stretch, anchor="w")
        scroll = ttk.Scrollbar(host, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True, padx=(8, 0))
        self.tree.bind("<Double-1>", self.preview_file)
        for kind, color in (("新建", theme.green), ("修改", theme.file_modify), ("删除", theme.red), ("读取", theme.muted)):
            self.tree.tag_configure(kind, foreground=color)
        self.files_empty = tk.Frame(host, bg=theme.bg)
        self.files_empty.place(relx=0.5, rely=0.52, anchor="center")
        label(self.files_empty, "▤", fg=theme.icon, size=25).pack()
        label(self.files_empty, "文件变更会出现在这里", fg=theme.muted, size=10).pack(pady=(10, 5))
        label(self.files_empty, "读取 · 创建 · 修改 · 删除", fg=theme.hint_soft, size=8).pack()
        label(parent, "双击条目，查看完整路径、内容与 Diff", fg=theme.hint, size=8,
              anchor="w").pack(fill="x", padx=15, pady=(5, 10))

    def _build_delivery(self, parent):
        toolbar = tk.Frame(parent, bg=theme.bg)
        toolbar.pack(side="bottom", fill="x", padx=12, pady=9)
        self.copy_button = ttk.Button(toolbar, text="复制", style="Ghost.TButton", command=self.copy_report, state="disabled")
        self.copy_button.pack(side="right")
        self.export_button = ttk.Button(toolbar, text="导出 .md", style="Ghost.TButton", command=self.export_report, state="disabled")
        self.export_button.pack(side="right", padx=4)
        host = tk.Frame(parent, bg=theme.bg)
        host.pack(fill="both", expand=True)
        self.delivery = text_box(host, height=7, background=theme.bg, font=(UI_FONT, 9))
        self.delivery_empty = tk.Frame(host, bg=theme.bg)
        self.delivery_empty.place(relx=0.5, rely=0.5, anchor="center")
        label(self.delivery_empty, "▧", fg=theme.icon, size=25).pack()
        label(self.delivery_empty, "每一步，都有交付", fg=theme.muted, size=10).pack(pady=(10, 5))
        self.delivery_hint = label(self.delivery_empty, "任务完成后查看输出的文件夹与文件", fg=theme.hint_soft, size=8)
        self.delivery_hint.pack()

    def _build_footer(self):
        tk.Frame(self.main_area, bg=theme.border, height=1).pack(fill="x")
        footer = tk.Frame(self.main_area, bg=theme.bg)
        footer.pack(fill="x", padx=18, pady=7)
        self.footer_status = tk.Label(footer, textvariable=self.status, bg=theme.bg, fg=theme.muted, font=(UI_FONT, 8), anchor="w")
        self.footer_status.pack(side="left", fill="x", expand=True)
        label(footer, "ClawX  /  Local Agent Studio", fg=theme.hint_soft, size=8).pack(side="right", padx=(8, 0))

    def _position_panes(self):
        if self.state.work_mode == "chat":
            self.workspace_panes.sashpos(0, int(self.workspace_panes.winfo_width() * 0.78))
        else:
            self.workspace_panes.sashpos(0, int(self.workspace_panes.winfo_width() * 0.64))
        self.inspector_panes.sashpos(0, int(self.inspector_panes.winfo_height() * 0.48))

    def _refresh_mode_and_thinking_styles(self):
        mode = self.state.work_mode
        instant = mode == "chat" and self.config.chat_style == "instant"
        level = self.config.effective_level()
        supported = self._active_levels() if hasattr(self, "_active_levels") else list(THINKING_LEVELS)
        if instant:
            levels = [item for item in INSTANT_LEVELS if item in supported] or [nearest_level("Medium", supported)]
            self.thinking_slider.set_levels(levels, INSTANT_LABELS)
        else:
            levels = [item for item in THINKING_LEVELS if item in supported] or list(THINKING_LEVELS)
            self.thinking_slider.set_levels(levels, None)
        for key, button in self.mode_buttons.items():
            active = key == mode
            bg = (theme.work_active_bg if key == "work" else theme.chat_active_bg) if active else theme.field
            fg = WORK_MODE_COLORS[key] if active else theme.muted
            button.configure(bg=bg, fg=fg, activebackground=bg, activeforeground=fg)
        for key, button in self.chat_style_buttons.items():
            active = key == self.config.chat_style
            bg, fg = (theme.chat_style_active_bg, theme.chat_style_active_fg) if active else (theme.field, theme.muted)
            button.configure(bg=bg, fg=fg, activebackground=bg, activeforeground=fg)
        if mode == "chat":
            self.chat_style_switch.pack(side="left", padx=(14, 0))
            self.scope_row.pack_forget()
        else:
            self.chat_style_switch.pack_forget()
            self.scope_row.pack(fill="x")
        self.composer.configure(height=154)
        self._lock_mode_controls(self.ui_busy)
        self.answer_hint.configure(text="快速简洁" if instant else "")
        self.thinking_menu_button.set_display(level, opened=self.thinking_menu_open, instant=instant)
        mode_text = WORK_MODE_LABELS[mode]
        if mode == "chat":
            mode_text += " · " + CHAT_STYLE_LABELS[self.config.chat_style]
        self.work_mode_badge.configure(text=mode_text, fg=WORK_MODE_COLORS[mode])
        self.placeholder.configure(text="输入问题，获得简洁回答…" if instant else
                                   "提出问题，一起深入讨论…" if mode == "chat" else "描述你的开发任务…")
        self.welcome_title.configure(text="今天想构建什么？" if mode == "work" else
                                     "一起把问题想透。" if not instant else "今天想聊点什么？")
        self.welcome_subtitle.configure(text="读写项目、执行验证、交付成果。" if mode == "work" else
                                        "即时回答，简洁直接。也可以切换深入讨论，展开分析。" if instant else
                                        "比较方案、梳理假设、探索细节。")
        suggestions = ([
            ("构建一个功能", "把想法变成可运行的代码", "请帮我实现一个功能："),
            ("检查与修复", "定位问题，整理修改方案", "请检查当前项目并提出修复建议。"),
            ("规划项目结构", "创建目录与初始文件", "请帮我规划并创建当前项目的文件结构。"),
            ("阅读并总结", "浏览代码并给出总结", "请阅读当前项目并总结结构与主要功能。"),
        ] if mode == "work" else [
            ("解释一个概念", "用容易理解的语言讲解", "请解释一个概念："),
            ("比较两种方案", "分析差异和适用场景", "请比较这两种方案："),
            ("梳理一个想法", "一起明确目标和下一步", "我有一个想法，帮我梳理："),
            ("讨论代码设计", "探讨结构、边界与取舍", "请与我讨论这个代码设计："),
        ])
        for parts, (title, description, prompt) in zip(self.suggestion_parts, suggestions):
            parts[3].configure(text=title)
            parts[4].configure(text=description)
            for widget in parts:
                widget.bind("<Button-1>", lambda _e, value=prompt: self.prefill(value))
        self.send_button.configure(style="Primary.TButton")
        self.welcome_mark.configure(fg=theme.accent)
        if mode == "chat":
            self.safety_label.configure(text="● 纯对话", fg=theme.green)
        else:
            self.safety_label.configure(text="● " + TIERS[self.state.tier-1], fg=TIER_COLORS[self.state.tier])
        if hasattr(self, "model_combo"):
            self.populate_model_menu()

    def toggle_thinking_menu(self):
        if self.thinking_menu_open:
            self.close_thinking_menu()
            return
        if self.ui_busy:
            return
        self.root.update_idletasks()
        width = min(370, max(250, self.main_area.winfo_width()-32))
        x = max(12, min(self.thinking_menu_button.winfo_rootx()-self.main_area.winfo_rootx(),
                        self.main_area.winfo_width()-width-12))
        y = max(8, self.thinking_menu_button.winfo_rooty()-self.main_area.winfo_rooty()-197)
        self.thinking_menu_open = True
        self.thinking_controls.place(x=x, y=y, width=width, height=190)
        self.thinking_controls.lift()
        self.thinking_menu_button.set_display(self.config.effective_level(), opened=True,
                                              instant=self.state.work_mode == "chat" and self.config.chat_style == "instant")
        self.thinking_slider.focus_set()

    def close_thinking_menu(self):
        self.thinking_menu_open = False
        if not getattr(self, "thinking_controls", None) or not self.thinking_controls.winfo_exists():
            return
        self.thinking_slider._cancel_drag()
        self.thinking_controls.place_forget()
        instant = self.state.work_mode == "chat" and self.config.chat_style == "instant"
        self.thinking_menu_button.set_display(self.config.effective_level(), instant=instant)

    def _dismiss_thinking_menu(self, event):
        if not self.thinking_menu_open:
            return
        widget = event.widget
        while widget is not None:
            if widget in (self.thinking_controls, self.thinking_menu_button):
                return
            widget = getattr(widget, "master", None)
        self.close_thinking_menu()

    def _thinking_window_resize(self, event):
        if event.widget == self.root and self.thinking_menu_open:
            self.close_thinking_menu()

    def _select_nav(self, key):
        self.nav_selection = key
        for name, button in self.nav_buttons.items():
            active = name == key
            color = theme.nav_active_bg if active else theme.sidebar
            button.configure(bg=color, fg=theme.fg if active else theme.muted)
            button.bind("<Leave>", lambda _e, item=button, bg=color: item.configure(bg=bg))

    def focus_panel(self, key):
        if key == "terminal":
            key = "chat"
        self._select_nav(key)
        if key == "chat":
            self.input.focus_set()
        else:
            self.workspace_panes.sashpos(0, int(self.workspace_panes.winfo_width() * 0.61))
            self.inspector_panes.sashpos(0, int(self.inspector_panes.winfo_height() * (0.65 if key == "files" else 0.30)))
            (self.tree if key == "files" else self.delivery).focus_set()

    def toggle_sidebar(self):
        self.sidebar_visible = not self.sidebar_visible
        if self.sidebar_visible:
            self.sidebar.pack(side="left", fill="y", before=self.main_area)
            self.sidebar_rule.pack(side="left", fill="y", before=self.main_area)
        else:
            self.sidebar.pack_forget()
            self.sidebar_rule.pack_forget()

    def prefill(self, value):
        if self.agent.running:
            return
        self.input.delete("1.0", "end")
        self.input.insert("1.0", value)
        self._update_placeholder()
        self.input.focus_set()
        self.input.mark_set("insert", "end-1c")

    def _update_placeholder(self, _event=None):
        if self.input.get("1.0", "end-1c"):
            self.placeholder.place_forget()
        else:
            self.placeholder.place(x=3, y=4)

    def _keyboard_send(self, _event=None):
        self.send()
        return "break"

    def _keyboard_newline(self, _event=None):
        if self.input.tag_ranges("sel"):
            self.input.delete("sel.first", "sel.last")
        self.input.insert("insert", "\n")
        self.input.see("insert")
        self._update_placeholder()
        return "break"

    def _escape(self, _event=None):
        if self.thinking_menu_open:
            self.close_thinking_menu()
            self.thinking_menu_button.focus_set()
            return "break"
        if self.agent.running and self.review is None:
            self.stop()

    def _status_changed(self, *_args):
        label = getattr(self, "activity_label", None)
        if label is None:
            return
        try:
            if not label.winfo_exists():
                return
        except tk.TclError:
            return
        value = self.status.get()
        waiting = "审批" in value and "等待" in value
        running = self.ui_busy
        title = "等待审批" if waiting else "执行中" if running else "就绪"
        color = theme.waiting if waiting else (theme.accent if running else theme.green)
        self.activity_label.configure(text="●  " + title, fg=color)

    def clear_console(self):
        set_text(self.console, "")

    def refresh_model_ui(self):
        entry = None
        if hasattr(self, "models"):
            entry = self.models.by_id(self.config.active_model_id)
        if entry and entry.get("label"):
            text = entry["label"][:28]
        else:
            text = self.config.model[:28] or "未配置模型"
        self.mode_label.configure(text=text)
        self.connection_label.configure(text="●  已配置 API" if self.config.api_key else "●  未配置 API Key")

    def refresh_workspace_ui(self):
        extra = len(self.state.allowed_roots)
        self.path_label.configure(text=self.state.root + (f"  + {extra} 个允许目录" if extra else ""))
        self.workspace_name.configure(text=self.state.project_name)
        self.workspace_path.configure(text=Path(self.state.root).name or self.state.root)
