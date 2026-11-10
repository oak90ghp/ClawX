"""State, skills, path policy, and the single authorization gate (no Tk calls)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable
import copy
import difflib
import json
import os
import queue
import re
import threading
import uuid


VALID_WORK_MODES = {"work", "chat"}
VALID_THINKING_LEVELS = {"Low", "Medium", "High", "Max", "Ultra"}
THINKING_LEVELS = ["Low", "Medium", "High", "Max", "Ultra"]
# Instant answers offer three compact levels instead of the full five.
INSTANT_LEVELS = ["Low", "Medium", "High"]
INSTANT_LABELS = {"Low": "标准", "Medium": "中", "High": "高"}
VALID_THEMES = {"dark", "light", "system"}
VALID_PROVIDERS = {"openai", "anthropic"}
REASONING_APIS = ("portable", "openai", "anthropic", "anthropic_manual")
EFFORT_CEILINGS = ("low", "medium", "high", "xhigh", "max")


def nearest_level(level: str, allowed) -> str:
    """The allowed level closest to `level` in slider order."""
    if level in allowed:
        return level
    targets = [THINKING_LEVELS.index(item) for item in allowed if item in THINKING_LEVELS]
    if not targets:
        return "Medium" if "Medium" in THINKING_LEVELS else THINKING_LEVELS[0]
    index = THINKING_LEVELS.index(level)
    return THINKING_LEVELS[min(targets, key=lambda position: abs(position - index))]


class PolicyError(RuntimeError):
    """A policy rejection that can safely be shown to the agent and user."""


class Cancelled(RuntimeError):
    """Cooperative cancellation."""


@dataclass(frozen=True)
class Operation:
    tool: str
    arguments: dict

    def as_dict(self) -> dict:
        return {"tool": self.tool, "arguments": copy.deepcopy(self.arguments)}

    def signature(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, ensure_ascii=False)


@dataclass
class PolicyState:
    root: str
    allowed_roots: tuple[str, ...] = ()
    project_name: str = "默认项目"
    project_prompt: str = ""
    tier: int = 2
    whole_computer: bool = False
    work_mode: str = "work"
    thinking_level: str = "Medium"
    revision: int = 0
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "root": self.root,
                "allowed_roots": list(self.allowed_roots),
                "project_name": self.project_name,
                "project_prompt": self.project_prompt,
                "tier": self.tier,
                "whole_computer": self.whole_computer,
                "work_mode": self.work_mode,
                "thinking_level": self.thinking_level,
                "revision": self.revision,
            }

    def update(self, **changes) -> None:
        with self.lock:
            bump_revision = False
            for key, value in changes.items():
                if key not in {"root", "allowed_roots", "project_name", "project_prompt",
                               "tier", "whole_computer", "work_mode", "thinking_level"}:
                    raise ValueError("Unknown policy setting")
                if key == "work_mode" and value not in VALID_WORK_MODES:
                    raise ValueError(f"Invalid work_mode: {value}")
                if key == "thinking_level" and value not in VALID_THINKING_LEVELS:
                    raise ValueError(f"Invalid thinking_level: {value}")
                if key == "allowed_roots":
                    value = tuple(value)
                setattr(self, key, value)
                if key in {"root", "allowed_roots", "tier", "whole_computer", "work_mode"}:
                    bump_revision = True
            if bump_revision:
                self.revision += 1


class ScopeValidator:
    """Canonical-path boundary. This is NOT an operating-system sandbox."""

    RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$", re.I)

    def __init__(self, state: PolicyState):
        self.state = state

    def resolve(self, value: str, *, cwd: bool = False, policy: dict | None = None) -> str:
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise PolicyError("路径必须是非空字符串，且不能包含 NUL。")
        policy = policy or self.state.snapshot()
        # Reject device namespaces, UNC shares, ADS, and DOS devices even in full scope.
        # Network access is intentionally not part of the file tool contract.
        raw = value.replace("/", "\\")
        if os.name == "nt":
            if raw.startswith("\\\\") or ":" in os.path.splitdrive(value)[1]:
                raise PolicyError("不支持网络路径、设备路径或 NTFS 备用数据流。")
            if any(self.RESERVED.match(p.rstrip(" .")) for p in raw.split("\\") if p):
                raise PolicyError("禁止访问 Windows 设备名。")
        root = os.path.realpath(os.path.abspath(policy["root"]))
        target = os.path.realpath(value if os.path.isabs(value) else os.path.join(root, value))
        if not policy["whole_computer"]:
            roots = [root, *(os.path.realpath(os.path.abspath(item))
                              for item in policy.get("allowed_roots", []))]
            inside = False
            for allowed in roots:
                try:
                    if os.path.commonpath([os.path.normcase(allowed), os.path.normcase(target)]) == os.path.normcase(allowed):
                        inside = True
                        break
                except ValueError:
                    continue
            if not inside:
                raise PolicyError(f"超出项目允许目录：{target}")
        if cwd and not os.path.isdir(target):
            raise PolicyError(f"工作目录不存在：{target}")
        return target


class ProjectManager:
    """Validated local project profiles with one primary and optional extra roots."""

    def __init__(self, path: Path, default_root: Path):
        self.path = path
        self.default_root = str(default_root.resolve())
        self.lock = threading.RLock()
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or set(raw) != {"active_id", "projects"}:
                raise ValueError("projects.json 格式无效")
            self.projects = [self.validate(item) for item in raw["projects"]]
            if not self.projects:
                raise ValueError("projects.json 至少需要一个项目")
            ids = {item["id"] for item in self.projects}
            if len(ids) != len(self.projects) or raw["active_id"] not in ids:
                raise ValueError("项目 ID 重复或 active_id 无效")
            self.active_id = raw["active_id"]
        else:
            self.projects = [self.validate({"id": uuid.uuid4().hex, "name": "默认项目",
                                             "root": self.default_root, "prompt": "",
                                             "allowed_folders": []})]
            self.active_id = self.projects[0]["id"]
            self._save()

    @staticmethod
    def validate(item: dict) -> dict:
        if not isinstance(item, dict) or set(item) != {"id", "name", "root", "prompt", "allowed_folders"}:
            raise ValueError("项目必须包含 id、name、root、prompt、allowed_folders")
        project_id, name, prompt = item["id"], item["name"], item["prompt"]
        if not isinstance(project_id, str) or not re.fullmatch(r"[a-f0-9]{32}", project_id):
            raise ValueError("项目 ID 无效")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise ValueError("项目名称必须为 1–80 个字符")
        if not isinstance(prompt, str) or len(prompt) > 20000:
            raise ValueError("项目 Prompt 最多 20000 个字符")
        if not isinstance(item["root"], str) or not item["root"].strip() or "\x00" in item["root"]:
            raise ValueError("项目主目录必须是非空本地路径")
        folders = item["allowed_folders"]
        if not isinstance(folders, list) or len(folders) > 32 or any(not isinstance(x, str) for x in folders):
            raise ValueError("额外允许目录必须是最多 32 项的字符串数组")
        ProjectManager._validate_local_paths([item["root"], *folders])
        root = os.path.realpath(os.path.abspath(item["root"]))
        if not os.path.isdir(root):
            raise ValueError(f"项目主目录不存在：{root}")
        canonical = []
        for folder in folders:
            resolved = os.path.realpath(os.path.abspath(folder))
            if not os.path.isdir(resolved):
                raise ValueError(f"允许目录不存在：{resolved}")
            if os.path.normcase(resolved) != os.path.normcase(root) and os.path.normcase(resolved) not in {
                    os.path.normcase(value) for value in canonical}:
                canonical.append(resolved)
        return {"id": project_id, "name": name.strip(), "root": root,
                "prompt": prompt.strip(), "allowed_folders": canonical}

    @staticmethod
    def _validate_local_paths(paths: list[str]) -> None:
        for value in paths:
            if not value.strip() or "\x00" in value:
                raise ValueError("项目目录必须是非空本地路径")
            if os.name == "nt":
                raw = value.replace("/", "\\")
                if raw.startswith("\\\\") or ":" in os.path.splitdrive(value)[1]:
                    raise ValueError("项目不支持网络路径、设备路径或 NTFS 备用数据流")
                if any(ScopeValidator.RESERVED.match(part.rstrip(" ."))
                       for part in raw.split("\\") if part):
                    raise ValueError("项目目录不能使用 Windows 设备名")

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"active_id": self.active_id, "projects": self.projects},
                                        ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)

    def all(self) -> list[dict]:
        with self.lock:
            return copy.deepcopy(self.projects)

    def active(self) -> dict:
        with self.lock:
            return copy.deepcopy(next(item for item in self.projects if item["id"] == self.active_id))

    def replace(self, projects: list[dict], active_id: str | None = None):
        checked = [self.validate(item) for item in projects]
        if not checked or len({item["id"] for item in checked}) != len(checked):
            raise ValueError("至少保留一个项目，且项目 ID 不能重复")
        if len({item["name"].casefold() for item in checked}) != len(checked):
            raise ValueError("项目名称不能重复")
        selected = active_id or self.active_id
        if selected not in {item["id"] for item in checked}:
            selected = checked[0]["id"]
        with self.lock:
            self.projects, self.active_id = checked, selected
            self._save()

    def select(self, project_id: str) -> dict:
        with self.lock:
            if project_id not in {item["id"] for item in self.projects}:
                raise ValueError("项目不存在")
            self.active_id = project_id
            self._save()
            return self.active()


class SkillManager:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.skills: list[dict] = []
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                raise ValueError("skills.json 必须是数组")
            self.skills = [self.validate(item) for item in data]

    @staticmethod
    def validate(item: dict) -> dict:
        if not isinstance(item, dict):
            raise ValueError("Skill 必须是 JSON 对象")
        out = {}
        for key in ("name", "description", "instruction"):
            value = item.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Skill 的 {key} 必须是非空字符串")
            out[key] = value.strip()
        allowed = item.get("allowed_tools", [])
        if not isinstance(allowed, list) or any(not isinstance(x, str) for x in allowed):
            raise ValueError("allowed_tools 必须是字符串数组")
        out["allowed_tools"] = allowed
        if not isinstance(item.get("enabled", True), bool):
            raise ValueError("enabled 必须是 JSON 布尔值")
        out["enabled"] = item.get("enabled", True)
        return out

    def all(self) -> list[dict]:
        with self.lock:
            return copy.deepcopy(self.skills)

    def replace(self, skills: list[dict]) -> None:
        checked = [self.validate(x) for x in skills]
        if len({x["name"].casefold() for x in checked}) != len(checked):
            raise ValueError("Skill 名称不能重复")
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(checked, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp, self.path)
            self.skills = checked

    def prompt(self) -> str:
        return json.dumps([x for x in self.all() if x["enabled"]], ensure_ascii=False)

    def check_tool(self, tool: str) -> None:
        # Enabled skills can narrow permissions but can never grant them.
        for item in self.all():
            if item["enabled"] and item["allowed_tools"] and tool not in item["allowed_tools"]:
                raise PolicyError(f"已启用 Skill「{item['name']}」不允许工具 {tool}")


@dataclass
class ApprovalRequest:
    kind: str
    title: str
    text: str
    editable: str
    revision: int
    token: str = field(default_factory=lambda: uuid.uuid4().hex)
    done: threading.Event = field(default_factory=threading.Event)
    answer: tuple[bool, str] | None = None


class ApprovalBroker:
    """Workers wait on Events; only the UI thread creates dialogs."""

    def __init__(self, events: queue.Queue, cancel: threading.Event):
        self.events = events
        self.cancel = cancel

    def ask(self, request: ApprovalRequest) -> str:
        self.events.put(("approval", request))
        while not request.done.wait(0.1):
            if self.cancel.is_set():
                raise Cancelled("用户已停止执行")
        if self.cancel.is_set():
            raise Cancelled("用户已停止执行")
        if not request.answer or not request.answer[0]:
            raise PolicyError("用户拒绝了本次操作或计划。")
        return request.answer[1]


def diff_preview(path: str, after: str | None) -> str:
    before = ""
    if os.path.isfile(path):
        with open(path, "rb") as handle:
            raw = handle.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise PolicyError("变更预览限制为 1 MiB，拒绝覆盖或删除更大的文件。")
        before = raw.decode("utf-8", errors="replace")
    diff = "".join(difflib.unified_diff(before.splitlines(True), (after or "").splitlines(True),
                                        fromfile=path + " (before)", tofile=path + " (after)"))
    return diff or "（内容无变化或空文件）"


class SecurityInterceptor:
    def __init__(self, state: PolicyState, scope: ScopeValidator, broker: ApprovalBroker,
                 auditor: Callable[[str], str], emit: Callable):
        self.state, self.scope, self.broker = state, scope, broker
        self.auditor, self.emit = auditor, emit
        self.plan: list[str] = []
        self.plan_revision = -1
        self.plan_index = 0

    def install_plan(self, operations: list[Operation], revision: int) -> None:
        if self.state.snapshot()["revision"] != revision:
            raise PolicyError("审批期间安全设置已改变，请重新制定计划。")
        self.plan = [x.signature() for x in operations]
        self.plan_revision, self.plan_index = revision, 0

    def invalidate(self) -> None:
        self.plan, self.plan_revision, self.plan_index = [], -1, 0

    def authorize(self, op: Operation, normalize: Callable[[Operation], Operation]) -> tuple[Operation, int]:
        policy = self.state.snapshot()
        if policy.get("work_mode") == "chat":
            raise PolicyError("当前处于「标准聊天 (Chat)」模式，禁止调用任何本地文件或终端命令工具。")
        rev, tier = policy["revision"], policy["tier"]
        if tier == 3:
            if rev != self.plan_revision or self.plan_index >= len(self.plan):
                raise PolicyError("档位 3 没有有效的已批准计划，或计划已执行完毕。")
            if op.signature() != self.plan[self.plan_index]:
                raise PolicyError(f"操作偏离已批准计划第 {self.plan_index + 1} 步，已硬性拦截。")
        elif op.tool in {"write_file", "delete_file", "run_command"}:
            details = json.dumps(op.as_dict(), ensure_ascii=False, indent=2)
            if op.tool in {"write_file", "delete_file"}:
                details += "\n\n" + diff_preview(op.arguments["path"], op.arguments.get("content"))
            if tier == 1:
                verdict = self.auditor(details)
                if verdict.strip() != "[SAFE]":
                    raise PolicyError("安全审查拦截：" + verdict[:2000])
                self.emit("chat", ("安全", "独立审查上下文返回 [SAFE]，允许本次操作。"))
            elif tier == 2:
                edited = self.broker.ask(ApprovalRequest(
                    "operation", "逐项操作审查", details,
                    json.dumps(op.as_dict(), ensure_ascii=False, indent=2), rev))
                try:
                    obj = json.loads(edited)
                    if not isinstance(obj, dict) or set(obj) != {"tool", "arguments"}:
                        raise ValueError("需要 tool 和 arguments")
                    op = normalize(Operation(obj["tool"], obj["arguments"]))
                except (ValueError, TypeError, KeyError) as exc:
                    raise PolicyError(f"编辑后的操作无效：{exc}") from exc
        if self.broker.cancel.is_set():
            raise Cancelled("用户已停止执行")
        if self.state.snapshot()["revision"] != rev:
            raise PolicyError("审批期间安全档位或范围已改变，本次授权失效，请重试。")
        return op, rev

    def consume(self, revision: int) -> None:
        if self.state.snapshot()["tier"] == 3 and revision == self.plan_revision:
            self.plan_index += 1


class ModelRegistry:
    """Persisted model list. Each model declares the thinking levels it supports.

    API keys are kept per model and stored locally in models.json so a
    user-provided LLM keeps working across restarts; settings.json never
    receives keys (see SECURITY.md).
    """

    KEYS = {"id", "label", "model", "provider", "base_url", "api_key",
            "reasoning_api", "effort_ceiling", "levels"}

    def __init__(self, path: Path, default: dict):
        self.path = path
        self.lock = threading.RLock()
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or set(raw) != {"active_id", "models"}:
                raise ValueError("models.json 格式无效")
            self.models = [self.validate(item) for item in raw["models"]]
            if not self.models:
                raise ValueError("models.json 至少需要一个模型")
            ids = {item["id"] for item in self.models}
            if len(ids) != len(self.models) or raw["active_id"] not in ids:
                raise ValueError("模型 ID 重复或 active_id 无效")
            self.active_id = raw["active_id"]
        else:
            self.models = [self.validate(default)]
            self.active_id = self.models[0]["id"]
            self._save()

    @staticmethod
    def validate(item: dict) -> dict:
        if not isinstance(item, dict) or set(item) != ModelRegistry.KEYS:
            raise ValueError("模型必须包含 id、label、model、provider、base_url、api_key、reasoning_api、effort_ceiling、levels")
        if not isinstance(item["id"], str) or not re.fullmatch(r"[a-f0-9]{32}", item["id"]):
            raise ValueError("模型 ID 无效")
        label = item["label"]
        if not isinstance(label, str) or not label.strip() or len(label.strip()) > 60:
            raise ValueError("模型名称必须为 1–60 个字符")
        if not isinstance(item["model"], str) or len(item["model"]) > 200:
            raise ValueError("模型 ID 必须是字符串")
        if not isinstance(item["base_url"], str) or len(item["base_url"]) > 400:
            raise ValueError("Base URL 无效")
        if item["provider"] not in VALID_PROVIDERS:
            raise ValueError("provider 无效")
        if not isinstance(item["api_key"], str) or len(item["api_key"]) > 2000:
            raise ValueError("API Key 必须是字符串")
        if item["reasoning_api"] not in REASONING_APIS:
            raise ValueError("思考参数接口无效")
        if item["effort_ceiling"] not in EFFORT_CEILINGS:
            raise ValueError("effort 上限无效")
        levels = item["levels"]
        if (not isinstance(levels, list) or not levels
                or any(x not in VALID_THINKING_LEVELS for x in levels)
                or len(set(levels)) != len(levels)):
            raise ValueError("支持的思考等级必须是非空且不重复的 Low/Medium/High/Max/Ultra 子集")
        ordered = [x for x in THINKING_LEVELS if x in levels]
        return {"id": item["id"], "label": label.strip(), "model": item["model"].strip(),
                "provider": item["provider"], "base_url": item["base_url"].strip(),
                "api_key": item["api_key"], "reasoning_api": item["reasoning_api"],
                "effort_ceiling": item["effort_ceiling"], "levels": ordered}

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"active_id": self.active_id, "models": self.models},
                                        ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)

    def all(self) -> list[dict]:
        with self.lock:
            return copy.deepcopy(self.models)

    def by_id(self, model_id: str):
        with self.lock:
            return copy.deepcopy(next((x for x in self.models if x["id"] == model_id), None))

    def active(self) -> dict:
        with self.lock:
            return copy.deepcopy(next((x for x in self.models if x["id"] == self.active_id),
                                      self.models[0]))

    def set_active(self, model_id: str) -> None:
        with self.lock:
            if model_id not in {x["id"] for x in self.models}:
                raise ValueError("模型不存在")
            self.active_id = model_id
            self._save()

    def replace(self, models: list[dict], active_id: str | None = None) -> None:
        checked = [self.validate(item) for item in models]
        if not checked or len({item["id"] for item in checked}) != len(checked):
            raise ValueError("至少保留一个模型，且模型 ID 不能重复")
        if len({item["label"].casefold() for item in checked}) != len(checked):
            raise ValueError("模型名称不能重复")
        selected = active_id or self.active_id
        if selected not in {item["id"] for item in checked}:
            selected = checked[0]["id"]
        with self.lock:
            self.models, self.active_id = checked, selected
            self._save()


class ConversationManager:
    """Persisted conversation records, grouped per project.

    Each record keeps the full message list (bounded), a title derived from the
    first user message, and create / update timestamps. Records never contain
    API keys or private reasoning chains — only user inputs and public replies.
    """

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.conversations: list[dict] = []
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or set(data) != {"conversations"}:
                raise ValueError("conversations.json 格式无效")
            self.conversations = [self.validate(item) for item in data["conversations"]]

    @staticmethod
    def validate(item: dict) -> dict:
        keys = {"id", "project_id", "title", "created", "updated", "messages"}
        branch_keys = {"parent_id", "branch_at"}
        if not isinstance(item, dict) or set(item) not in (keys, keys | branch_keys):
            raise ValueError("对话记录字段不完整")
        if not isinstance(item["id"], str) or not re.fullmatch(r"[a-f0-9]{32}", item["id"]):
            raise ValueError("对话 ID 无效")
        if not isinstance(item["project_id"], str) or not item["project_id"].strip():
            raise ValueError("对话必须属于一个项目")
        if not isinstance(item["title"], str) or len(item["title"]) > 80:
            raise ValueError("对话标题无效")
        if not isinstance(item["created"], str) or not isinstance(item["updated"], str):
            raise ValueError("对话时间戳无效")
        messages = item["messages"]
        if not isinstance(messages, list) or len(messages) > 200:
            raise ValueError("对话消息必须是最多 200 项的数组")
        cleaned = []
        for message in messages:
            if (not isinstance(message, dict) or set(message) != {"role", "content", "mode"}
                    or message["role"] not in {"user", "assistant"}
                    or message["mode"] not in VALID_WORK_MODES
                    or not isinstance(message["content"], str) or len(message["content"]) > 30000):
                raise ValueError("对话消息格式无效")
            cleaned.append({"role": message["role"], "content": message["content"],
                            "mode": message["mode"]})
        result = {"id": item["id"], "project_id": item["project_id"], "title": item["title"],
                  "created": item["created"], "updated": item["updated"], "messages": cleaned}
        if "parent_id" in item:
            if (not isinstance(item["parent_id"], str)
                    or not re.fullmatch(r"[a-f0-9]{32}", item["parent_id"])
                    or item["parent_id"] == item["id"]
                    or type(item["branch_at"]) is not int or not 0 <= item["branch_at"] < 200):
                raise ValueError("分支来源或消息位置无效")
            result.update(parent_id=item["parent_id"], branch_at=item["branch_at"])
        return result

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"conversations": self.conversations},
                                        ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)

    def all(self, project_id: str | None = None) -> list[dict]:
        with self.lock:
            items = [item for item in self.conversations
                     if project_id is None or item["project_id"] == project_id]
            return copy.deepcopy(sorted(items, key=lambda x: x["updated"], reverse=True))

    def by_id(self, conversation_id: str):
        with self.lock:
            return copy.deepcopy(next((x for x in self.conversations if x["id"] == conversation_id), None))

    def upsert(self, conversation: dict) -> None:
        checked = self.validate(conversation)
        with self.lock:
            self.conversations = [x for x in self.conversations if x["id"] != checked["id"]]
            self.conversations.append(checked)
            self._save()

    def delete(self, conversation_id: str) -> None:
        with self.lock:
            self.conversations = [x for x in self.conversations if x["id"] != conversation_id]
            self._save()

    def fork(self, conversation_id: str, message_index: int, title: str | None = None) -> dict:
        """Copy an inclusive message prefix; branches survive deletion of their source."""
        with self.lock:
            source = self.by_id(conversation_id)
            if source is None:
                raise ValueError("原对话不存在")
            if type(message_index) is not int or not 0 <= message_index < len(source["messages"]):
                raise ValueError("请选择有效的分支消息")
            if title is None:
                title = (source["title"] or "未命名对话")[:75] + " · 分支"
            if not isinstance(title, str) or not title.strip():
                raise ValueError("分支名称不能为空")
            now = datetime.now().isoformat(timespec="seconds")
            branch = self.validate({"id": uuid.uuid4().hex, "project_id": source["project_id"],
                                    "title": title.strip(), "created": now, "updated": now,
                                    "messages": source["messages"][:message_index + 1],
                                    "parent_id": source["id"], "branch_at": message_index})
            self.upsert(branch)
            return copy.deepcopy(branch)
