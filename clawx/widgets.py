"""Shared palette, thinking/mode constants, markdown rendering, and UI primitives."""
import itertools
import re
import tkinter as tk
import webbrowser
from tkinter import ttk
from tkinter import font as tkfont

from .core import INSTANT_LABELS, THINKING_LEVELS, VALID_THEMES, nearest_level


UI_FONT, CODE_FONT = "Microsoft YaHei UI", "Cascadia Code"
TIERS = ["1 · AI 独立审查", "2 · 用户逐项审批", "3 · 审批计划并锁定", "4 · 全部允许"]

# Main modes and nested chat styles.
WORK_MODES = ("work", "chat")
WORK_MODE_LABELS = {"work": "开发", "chat": "聊天"}
CHAT_STYLE_LABELS = {"instant": "即时回答", "deep": "深入讨论"}


DARK_PALETTE = {
    "bg": "#1b1b1b", "panel": "#252525", "field": "#202020",
    "sidebar": "#151515", "border": "#343434", "hover": "#30302e",
    "fg": "#eeeae4", "muted": "#99968f", "accent": "#d5a183",
    "green": "#9fc5aa", "red": "#eaa09b",
    "select": "#443a32", "select_bg": "#3a332d", "text_select": "#4b443c",
    "disabled_fg": "#65635f",
    "primary_fg": "#251d17", "primary_active": "#e1b69d",
    "primary_disabled_bg": "#40372f", "primary_disabled_fg": "#8d7d70",
    "scrollbar": "#45443f", "scrollbar_active": "#67635d",
    "section": "#6f6d68", "slash": "#5f5c57", "console_bg": "#181818",
    "placeholder": "#85817a", "hint": "#77736d", "hint_soft": "#706d67",
    "icon": "#68645e",
    "user_bubble": "#2c2925", "thinking_fg": "#b8c4d0", "thinking_bg": "#212429",
    "tool_fg": "#c0b5a9",
    "work_active_bg": "#3b2e25", "chat_active_bg": "#1e2f3d",
    "chat_style_active_bg": "#35313c", "chat_style_active_fg": "#e9d5ff",
    "nav_active_bg": "#2c2824", "card_hover_border": "#675244",
    "diff_add_bg": "#223027", "diff_remove_bg": "#362525", "diff_hunk_fg": "#b5b0d0",
    "menu_hover_border": "#514a58",
    "tier1": "#a8bed2", "tier3": "#c1b1d4", "tier4": "#e5b587",
    "file_modify": "#e2c293", "waiting": "#e2c293", "mode_chat": "#7dd3fc",
    "thinking_low": "#94a3b8", "thinking_medium": "#9fc5aa", "thinking_high": "#e2c293",
    "thinking_max": "#f59e0b", "thinking_ultra": "#c084fc",
    "ultra_text": ("#8b5cf6", "#a855f7", "#c084fc", "#e879f9", "#f0abfc"),
}

LIGHT_PALETTE = {
    "bg": "#f6f4f1", "panel": "#ffffff", "field": "#ece9e3",
    "sidebar": "#e9e5de", "border": "#d8d2c8", "hover": "#e7e2da",
    "fg": "#292521", "muted": "#6f6a63", "accent": "#a85f2a",
    "green": "#3f7d4e", "red": "#b8493f",
    "select": "#d3cbbf", "select_bg": "#ddd6cc", "text_select": "#c9bfae",
    "disabled_fg": "#b3ada4",
    "primary_fg": "#ffffff", "primary_active": "#96521f",
    "primary_disabled_bg": "#ddd6cc", "primary_disabled_fg": "#c0b9ae",
    "scrollbar": "#bcb6aa", "scrollbar_active": "#a69f92",
    "section": "#8b857c", "slash": "#aaa39a", "console_bg": "#f0eee9",
    "placeholder": "#a29c92", "hint": "#8b857c", "hint_soft": "#969088",
    "icon": "#b3ada3",
    "user_bubble": "#e7e1d6", "thinking_fg": "#4e5a66", "thinking_bg": "#e3eaf0",
    "tool_fg": "#665e54",
    "work_active_bg": "#f2ded1", "chat_active_bg": "#d9e8f4",
    "chat_style_active_bg": "#e8e0f0", "chat_style_active_fg": "#6a4a8f",
    "nav_active_bg": "#ddd8cf", "card_hover_border": "#b07e5e",
    "diff_add_bg": "#e2efe5", "diff_remove_bg": "#f5e1df", "diff_hunk_fg": "#6b6593",
    "menu_hover_border": "#b9a8c2",
    "tier1": "#3f6f9e", "tier3": "#75589e", "tier4": "#b06f38",
    "file_modify": "#a97a1e", "waiting": "#a97a1e", "mode_chat": "#0e7490",
    "thinking_low": "#5b7086", "thinking_medium": "#3f7d4e", "thinking_high": "#a97a1e",
    "thinking_max": "#c2710a", "thinking_ultra": "#7e3fb5",
    "ultra_text": ("#6d28d9", "#7c3aed", "#8b5cf6", "#a21caf", "#be185d"),
}

# Mutable semantic colors; widgets read them at creation time. theme.apply() swaps
# the values for dark / light mode and updates the shared accent dictionaries.
TIER_COLORS = {1: DARK_PALETTE["tier1"], 2: DARK_PALETTE["green"],
               3: DARK_PALETTE["tier3"], 4: DARK_PALETTE["tier4"]}
WORK_MODE_COLORS = {"work": DARK_PALETTE["accent"], "chat": DARK_PALETTE["mode_chat"]}
THINKING_COLORS = {level: DARK_PALETTE[f"thinking_{level.lower()}"] for level in THINKING_LEVELS}


class Theme:
    """Current palette. Apply a mode before building widgets; the app rebuilds
    its UI on a mode change and replays logs to restore content."""

    def __init__(self):
        self.mode = "dark"
        self._set(DARK_PALETTE)

    def _set(self, source: dict):
        for key, value in source.items():
            setattr(self, key, value)

    def apply(self, mode: str):
        if mode not in VALID_THEMES:
            raise ValueError(f"Unknown theme: {mode}")
        self.mode = mode
        self._set(LIGHT_PALETTE if mode == "light" else DARK_PALETTE)
        TIER_COLORS[1], TIER_COLORS[2] = self.tier1, self.green
        TIER_COLORS[3], TIER_COLORS[4] = self.tier3, self.tier4
        WORK_MODE_COLORS["work"] = self.accent
        WORK_MODE_COLORS["chat"] = self.mode_chat
        for level in THINKING_LEVELS:
            THINKING_COLORS[level] = getattr(self, f"thinking_{level.lower()}")
        ThinkingMenuButton.ULTRA_TEXT = self.ultra_text


theme = Theme()


def text_box(parent, *, editable=False, height=10, wrap="word", background=None,
             font=None, padx=14, pady=12) -> tk.Text:
    background = background or theme.field
    frame = tk.Frame(parent, bg=background)
    frame.pack(fill="both", expand=True)
    text = tk.Text(frame, bg=background, fg=theme.fg, insertbackground=theme.fg,
                   selectbackground=theme.text_select,
                   font=font or (CODE_FONT, 10), relief="flat", borderwidth=0, highlightthickness=0,
                   padx=padx, pady=pady, wrap=wrap, spacing1=2, spacing3=3,
                   height=height, undo=editable, state="normal" if editable else "disabled")
    scrollbar = ttk.Scrollbar(frame, command=text.yview)
    text.configure(yscrollcommand=scrollbar.set)
    scrollbar.pack(side="right", fill="y")
    text.pack(side="left", fill="both", expand=True)
    if wrap == "none":
        horizontal = ttk.Scrollbar(parent, orient="horizontal", command=text.xview)
        horizontal.pack(fill="x")
        text.configure(xscrollcommand=horizontal.set)
    return text


_LINK_COUNTER = itertools.count(1)
_MD_PATTERN = re.compile(r"(`[^`\n]+`|\*\*[^*\n]+\*\*|\*[^*\n]+\*|\[[^\]\n]+\]\([^)\n]+\))")


def _insert_inline(widget: tk.Text, segment: str, base):
    position = 0
    for match in _MD_PATTERN.finditer(segment):
        if match.start() > position:
            widget.insert("end", segment[position:match.start()], base)
        token = match.group(0)
        if token.startswith("`") and token.endswith("`") and len(token) > 2:
            widget.insert("end", token[1:-1], "md_inline")
        elif token.startswith("**") and token.endswith("**") and len(token) > 4:
            widget.insert("end", token[2:-2], "md_bold")
        elif token.startswith("*") and token.endswith("*") and len(token) > 2:
            widget.insert("end", token[1:-1], "md_italic")
        else:
            label, url = token[1:-1].split("](", 1)
            tag = f"mdlink_{id(widget)}_{next(_LINK_COUNTER)}"
            widget.tag_configure(tag, foreground=theme.accent, underline=True, font=(UI_FONT, 10))
            widget.tag_bind(tag, "<Button-1>", lambda _e, target=url: webbrowser.open(target))
            widget.tag_bind(tag, "<Enter>", lambda _e: widget.configure(cursor="hand2"))
            widget.tag_bind(tag, "<Leave>", lambda _e: widget.configure(cursor=""))
            widget.insert("end", label, tag)
        position = match.end()
    if position < len(segment):
        widget.insert("end", segment[position:], base)


def render_markdown(widget: tk.Text, text: str, *, base=None, clear=False):
    """Insert markdown-ish text with headings, emphasis, inline code, fenced code
    blocks, quotes, lists, rules, and clickable links. Tags are (re)configured
    from the current theme; pass clear=True to replace existing content."""
    widget.tag_configure("md_h1", font=(UI_FONT, 11, "bold"), foreground=theme.accent,
                         spacing1=10, spacing3=4)
    widget.tag_configure("md_h2", font=(UI_FONT, 10, "bold"), foreground=theme.fg,
                         spacing1=8, spacing3=3)
    widget.tag_configure("md_h3", font=(UI_FONT, 9, "bold"), foreground=theme.muted,
                         spacing1=6, spacing3=3)
    widget.tag_configure("md_bold", font=(UI_FONT, 10, "bold"))
    widget.tag_configure("md_italic", font=(UI_FONT, 10, "italic"))
    widget.tag_configure("md_inline", font=(CODE_FONT, 9), foreground=theme.tool_fg,
                         background=theme.field)
    widget.tag_configure("md_code", font=(CODE_FONT, 9), foreground=theme.fg,
                         background=theme.field, lmargin1=14, lmargin2=14, rmargin=10,
                         spacing1=4, spacing3=8)
    widget.tag_configure("md_quote", foreground=theme.muted, font=(UI_FONT, 9, "italic"),
                         lmargin1=14, lmargin2=14, spacing3=4)
    widget.tag_configure("md_rule", foreground=theme.border, spacing1=6, spacing3=6)
    widget.tag_configure("md_list", lmargin1=16, lmargin2=26, spacing3=2)
    base = base or ""
    state = str(widget.cget("state"))
    widget.configure(state="normal")
    if clear:
        widget.delete("1.0", "end")
    lines = text.split("\n")
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if stripped.startswith("```"):
            index += 1
            block = []
            while index < len(lines) and not lines[index].strip().startswith("```"):
                block.append(lines[index])
                index += 1
            index += 1
            widget.insert("end", "\n".join(block) + "\n", "md_code")
            continue
        if not stripped:
            widget.insert("end", "\n")
            index += 1
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            level = min(len(heading.group(1)), 3)
            widget.insert("end", heading.group(2) + "\n", f"md_h{level}")
            index += 1
            continue
        if re.match(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$", line):
            widget.insert("end", "─" * 28 + "\n", "md_rule")
            index += 1
            continue
        if stripped.startswith(">"):
            widget.insert("end", stripped[1:].strip() + "\n", "md_quote")
            index += 1
            continue
        bullet = re.match(r"^\s*([-*])\s+(.*)$", line)
        numbered = re.match(r"^\s*\d+[.)]\s+(.*)$", line)
        if bullet:
            _insert_inline(widget, "•  " + bullet.group(2), "md_list")
        elif numbered:
            _insert_inline(widget, "•  " + numbered.group(1), "md_list")
        else:
            _insert_inline(widget, line, base)
        widget.insert("end", "\n")
        index += 1
    widget.configure(state=state)


class ThinkingSlider(tk.Canvas):
    """Discrete stops with drag preview and keyboard input; no animated beams.

    `levels` selects which stops exist (5 for work/deep, 3 for instant) and
    `allowed` restricts selectable stops per model support. Unsupported stops
    are drawn dimmed and interactions snap to the nearest allowed stop.
    """

    def __init__(self, parent, *, level="Medium", command=None, background=None,
                 levels=None, labels=None, allowed=None, **kwargs):
        super().__init__(parent, height=66, width=280, bg=background or theme.panel,
                         highlightthickness=1, highlightbackground=background or theme.panel,
                         takefocus=True, borderwidth=0, **kwargs)
        self.command = command
        self.enabled = True
        self.levels = list(levels or THINKING_LEVELS)
        self.labels = dict(labels or {})
        self.allowed = set(allowed) if allowed else set(self.levels)
        if level not in self.levels:
            level = nearest_level(level, self.levels)
        if level not in self.allowed:
            level = nearest_level(level, self.allowed)
        self.level = level
        self._dragging = False
        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<Button-1>", self._press)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Escape>", self._cancel_drag)
        self.bind("<FocusIn>", lambda _e: self.configure(highlightbackground=theme.border))
        self.bind("<FocusOut>", lambda _e: self.configure(highlightbackground=self.cget("bg")))
        for key, offset in (("Left", -1), ("Down", -1), ("Right", 1), ("Up", 1)):
            self.bind("<" + key + ">", lambda _e, step=offset: self._key(step))
        self.bind("<Home>", lambda _e: self._choose_edge(0))
        self.bind("<End>", lambda _e: self._choose_edge(-1))
        self._draw()

    def _bounds(self):
        return 26, max(80, self.winfo_width() - 26)

    def _allowed_indices(self):
        return [self.levels.index(item) for item in self.levels if item in self.allowed]

    def _snap(self, index):
        candidates = self._allowed_indices() or [self.levels.index(self.level)]
        return min(candidates, key=lambda position: abs(position - index))

    def _draw(self):
        self.delete("all")
        left, right = self._bounds()
        y = 23
        count = len(self.levels)
        selected = self.levels.index(self.level)
        color = THINKING_COLORS.get(self.level, theme.muted) if self.enabled else theme.muted
        allowed = self._allowed_indices()
        self.create_line(left, y, right, y, fill=theme.border, width=4, capstyle="round")
        position = left + (right - left) * selected / max(1, count - 1)
        if selected:
            self.create_line(left, y, position, y, fill=color, width=4, capstyle="round")
        for index, title in enumerate(self.levels):
            x = left + (right - left) * index / max(1, count - 1)
            supported = index in allowed
            display = self.labels.get(title, title)
            self.create_oval(x-3, y-3, x+3, y+3,
                             fill=color if index <= selected else theme.border, outline="")
            self.create_text(x, 49, text=display,
                             fill=color if index == selected else (theme.muted if supported else theme.disabled_fg),
                             font=(UI_FONT, 8, "bold" if index == selected else "normal"))
        self.create_oval(position-7, y-7, position+7, y+7, fill=theme.fg, outline=color, width=2)
        self.configure(cursor="hand2" if self.enabled else "arrow")

    def set_level(self, level):
        if level not in self.levels:
            raise ValueError("Unknown thinking level")
        self.level = level
        self._draw()

    def set_levels(self, levels, labels=None):
        self.levels = list(levels) or list(THINKING_LEVELS)
        self.labels = dict(labels or {})
        if self.level not in self.levels:
            self.level = nearest_level(self.level, self.levels)
        self._draw()

    def set_allowed(self, allowed):
        self.allowed = set(allowed) or set(self.levels)
        if self.level not in self.allowed:
            self.level = nearest_level(self.level, self.allowed)
        self._draw()

    def set_enabled(self, enabled):
        if not enabled and self._dragging:
            self._cancel_drag()
        self.enabled = bool(enabled)
        self._draw()

    def _index_at(self, x):
        left, right = self._bounds()
        count = max(1, len(self.levels) - 1)
        return max(0, min(len(self.levels) - 1, int((x-left) * count / (right-left) + 0.5)))

    def _press(self, event):
        if self.enabled:
            self.focus_set()
            self._before_drag = self.level
            self._dragging = True
            self.set_level(self.levels[self._snap(self._index_at(event.x))])

    def _drag(self, event):
        if self.enabled and self._dragging:
            self.set_level(self.levels[self._snap(self._index_at(event.x))])

    def _release(self, event):
        if self.enabled and self._dragging:
            self._dragging = False
            self._choose(self._snap(self._index_at(event.x)))

    def _cancel_drag(self, _event=None):
        if self._dragging:
            self._dragging = False
            self.set_level(self._before_drag)
            return "break"

    def _key(self, step):
        order = self._allowed_indices() or [self.levels.index(self.level)]
        current = self.levels.index(self.level)
        try:
            position = order.index(current)
        except ValueError:
            position = min(range(len(order)), key=lambda i: abs(order[i] - current))
        target = order[min(len(order) - 1, max(0, position + step))]
        return self._choose(target)

    def _choose_edge(self, edge):
        order = self._allowed_indices() or [self.levels.index(self.level)]
        return self._choose(order[edge])

    def _choose(self, index):
        if self.enabled:
            self.set_level(self.levels[max(0, min(len(self.levels) - 1, index))])
            if self.command:
                self.command(self.level)
        return "break"


class ThinkingMenuButton(tk.Canvas):
    """Composer menu button whose selected level uses the profile color."""

    ULTRA_TEXT = DARK_PALETTE["ultra_text"]

    def __init__(self, parent, command):
        super().__init__(parent, width=150, height=30, bg=theme.panel, highlightthickness=0,
                         borderwidth=0, takefocus=True, cursor="hand2")
        self.command, self.enabled = command, True
        self.level, self.opened, self.instant = "Medium", False, False
        self._font = tkfont.Font(family=UI_FONT, size=9)
        self._bold = tkfont.Font(family=UI_FONT, size=9, weight="bold")
        self.bind("<Button-1>", self._invoke)
        self.bind("<Return>", self._invoke)
        self.bind("<space>", self._invoke)
        self.bind("<Enter>", lambda _e: self._draw(True))
        self.bind("<Leave>", lambda _e: self._draw(False))
        self.bind("<FocusIn>", lambda _e: self._draw(True))
        self.bind("<FocusOut>", lambda _e: self._draw(False))
        self._draw(False)

    def set_display(self, level, *, opened=False, instant=False):
        self.level, self.opened, self.instant = level, bool(opened), bool(instant)
        self._draw(False)

    def set_enabled(self, enabled):
        self.enabled = bool(enabled)
        self.configure(cursor="hand2" if self.enabled else "arrow")
        self._draw(False)

    def _invoke(self, _event=None):
        if self.enabled:
            self.command()
        return "break"

    def _draw(self, hover):
        self.delete("all")
        border = theme.menu_hover_border if hover and self.enabled else theme.border
        self.create_rectangle(1, 1, 149, 29, fill=theme.field, outline=border)
        prefix = "思考 · "
        if self.instant:
            label = INSTANT_LABELS.get(self.level, self.level)
            self.create_text(12, 15, text=prefix, anchor="w", fill=theme.muted, font=self._font)
            x = 12 + self._font.measure(prefix)
            self.create_text(x, 15, text=label, anchor="w", fill=theme.green, font=self._bold)
            x += self._bold.measure(label)
            self.create_text(min(138, x+10), 15, text="▴" if self.opened else "▾",
                             fill=theme.muted, font=self._font)
            return
        self.create_text(12, 15, text=prefix, anchor="w", fill=theme.muted, font=self._font)
        x = 12 + self._font.measure(prefix)
        if self.level == "Ultra":
            for index, character in enumerate(self.level):
                self.create_text(x, 15, text=character, anchor="w",
                                 fill=self.ULTRA_TEXT[index], font=self._bold)
                x += self._bold.measure(character)
        else:
            self.create_text(x, 15, text=self.level, anchor="w",
                             fill=THINKING_COLORS[self.level], font=self._bold)
            x += self._bold.measure(self.level)
        self.create_text(min(138, x+10), 15, text="▴" if self.opened else "▾",
                         fill=theme.muted, font=self._font)


class RoundedPanel(tk.Canvas):
    """Rounded input container without animated borders or light beams."""

    def __init__(self, parent, *, fill=None, outline=None, radius=16, **kwargs):
        super().__init__(parent, bg=parent.cget("background"), highlightthickness=0,
                         borderwidth=0, **kwargs)
        self.fill, self.outline, self.radius = fill or theme.panel, outline or theme.border, radius
        self.body = tk.Frame(self, bg=self.fill)
        self.shape = self.create_polygon([0, 0, 1, 0, 1, 1], smooth=True, splinesteps=24,
                                         fill=self.fill, outline=self.outline, width=1)
        self.content = self.create_window(12, 12, anchor="nw", window=self.body)
        self.bind("<Configure>", self._resize)

    def _resize(self, event):
        w, h, r = event.width-1, event.height-1, self.radius
        self.coords(self.shape, r, 1, w-r, 1, w, 1, w, r, w, h-r, w, h,
                    w-r, h, r, h, 1, h, 1, h-r, 1, r, 1, 1)
        self.itemconfigure(self.content, width=max(1, w-26), height=max(1, h-24))

    def focus_border(self, active):
        self.itemconfigure(self.shape, outline=theme.accent if active else self.outline)


def set_text(widget: tk.Text, value: str, *, append=False):
    state = str(widget.cget("state"))
    widget.configure(state="normal")
    if not append:
        widget.delete("1.0", "end")
    widget.insert("end", value)
    # Bounded UI logs: full current deliverable is kept separately by the app.
    if append and int(widget.index("end-1c").split(".")[0]) > 6000:
        widget.delete("1.0", "1500.0")
    widget.see("end")
    widget.configure(state=state)
