"""Validated tools, isolated portable command helpers, and cancellable processes."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import codecs
import json
import locale
import os
import queue
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time

from .core import Cancelled, Operation, PolicyError, ScopeValidator, diff_preview


# Only this constant program runs in scoped command mode. No user code is eval'd.
# -I prevents PYTHONPATH, current-directory modules and user site injection.
COMMAND_HELPER = r'''
import hashlib, json, os, sys
action, args = json.loads(sys.argv[1])
if action == 'pwd':
    print(os.getcwd())
elif action == 'echo':
    print(' '.join(args))
elif action == 'ls':
    for name in sorted(os.listdir(args[0]))[:1000]:
        print(name)
elif action in ('cat', 'check_python', 'sha256'):
    with open(args[0], 'rb') as handle:
        raw = handle.read(1048577)
    if len(raw) > 1048576:
        raise ValueError('File exceeds 1 MiB')
    if action == 'cat':
        print(raw.decode('utf-8'))
    elif action == 'sha256':
        print(hashlib.sha256(raw).hexdigest())
    else:
        compile(raw, args[0], 'exec')
        print('Python syntax OK (no code executed, no bytecode written)')
'''


def split_command(command: str) -> list[str]:
    try:
        # A deliberately small portable grammar, not a shell grammar.
        lexer = shlex.shlex(command, posix=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        lexer.escape = ""  # Preserve Windows backslashes inside and outside quotes.
        words = list(lexer)
    except ValueError as exc:
        raise PolicyError(f"命令引号不完整：{exc}") from exc
    if not words:
        raise PolicyError("命令不能为空。")
    return words


class ToolExecutor:
    LIMIT = 1024 * 1024

    def __init__(self, state, scope, security, skills, cancel, emit):
        self.state, self.scope, self.security, self.skills = state, scope, security, skills
        self.cancel, self.emit = cancel, emit
        self.records: list[dict] = []

    def scoped_command(self, command: str, cwd: str) -> tuple[str, list[str]]:
        words = split_command(command)
        name, args = words[0], words[1:]
        counts = {"pwd": (0, 0), "echo": (0, 200), "ls": (0, 1),
                  "cat": (1, 1), "check_python": (1, 1), "sha256": (1, 1)}
        if name not in counts:
            raise PolicyError("指定目录模式仅支持 pwd、ls、cat、echo、check_python、sha256；任意 Shell 需要整个计算机模式。")
        low, high = counts[name]
        if not low <= len(args) <= high:
            raise PolicyError(f"{name} 的参数数量应为 {low}..{high}。")
        if name in {"ls", "cat", "check_python", "sha256"}:
            argument = args[0] if args else "."
            target = argument if os.path.isabs(argument) else os.path.join(cwd, argument)
            args = [self.scope.resolve(target, cwd=name == "ls")]
        return name, args

    def portable_mode(self) -> bool:
        policy = self.state.snapshot()
        # A command string alone cannot constrain an arbitrary shell's file effects.
        # Strict plans therefore always use the bounded, non-evaluating helper.
        return not policy["whole_computer"] or policy["tier"] == 3

    def normalize(self, op: Operation) -> Operation:
        if self.state.snapshot().get("work_mode") == "chat":
            raise PolicyError("标准聊天禁止调用本地文件或终端工具。")
        if not isinstance(op.tool, str) or not isinstance(op.arguments, dict):
            raise PolicyError("工具名必须是字符串，arguments 必须是对象。")
        required = {"read_file": {"path"}, "list_files": {"path"}, "write_file": {"path", "content"},
                    "delete_file": {"path"}, "run_command": {"command", "cwd"}}
        if op.tool not in required or set(op.arguments) != required[op.tool]:
            raise PolicyError("未知工具，或工具参数不完整/含未声明字段。")
        self.skills.check_tool(op.tool)
        args = dict(op.arguments)
        if any(not isinstance(value, str) for value in args.values()):
            raise PolicyError("工具参数值必须是字符串。")
        if "path" in args:
            args["path"] = self.scope.resolve(args["path"], cwd=op.tool == "list_files")
        if op.tool == "write_file":
            if len(args["content"].encode("utf-8")) > self.LIMIT:
                raise PolicyError("写入内容超过 1 MiB。")
            if not os.path.isdir(os.path.dirname(args["path"])):
                raise PolicyError("父目录必须已存在；请在已有目录内创建文件。")
        if op.tool == "run_command":
            if not args["command"].strip() or len(args["command"]) > 16000:
                raise PolicyError("命令不能为空或超过 16000 字符。")
            args["cwd"] = self.scope.resolve(args["cwd"], cwd=True)
            if self.portable_mode():
                action, operands = self.scoped_command(args["command"], args["cwd"])
                args["command"] = " ".join([action] + [shlex.quote(x.replace("\\", "/") if action != "echo" else x)
                                                       for x in operands])
        return Operation(op.tool, args)

    def record(self, path: str, kind: str, preview: str, **extra) -> None:
        item = {"path": path, "kind": kind, "time": datetime.now().strftime("%H:%M:%S"),
                "preview": preview, **extra}
        self.records.append(item)
        self.emit("file", item)

    def execute(self, requested: Operation) -> dict:
        if self.cancel.is_set():
            raise Cancelled("用户已停止执行")
        self.emit("chat", ("工具", json.dumps(requested.as_dict(), ensure_ascii=False)))
        try:
            op = self.normalize(requested)
            op, revision = self.security.authorize(op, self.normalize)
            with self.state.lock:
                if self.state.revision != revision:
                    raise PolicyError("安全设置改变，授权失效。")
                checked = self.normalize(op)
                if checked.signature() != op.signature():
                    raise PolicyError("目标路径在审批后发生变化，拒绝执行。")
                args, tool = op.arguments, op.tool
                if tool == "run_command":
                    # Popen occurs under the same policy lock; waiting occurs outside it.
                    process = self.start_command(args)
                    result = None
                else:
                    process = None
                    result = self.file_operation(op)
                self.security.consume(revision)
            if process is not None:
                result = self.wait_command(process, args, revision)
                if not result["returncode"] and not result["terminated"] and self.portable_mode():
                    action, operands = self.scoped_command(args["command"], args["cwd"])
                    if action in {"cat", "check_python", "sha256", "ls"}:
                        self.record(operands[0], "命令列目录" if action == "ls" else "命令读取", result["output"])
            return result
        except PolicyError as exc:
            self.emit("chat", ("拦截", str(exc)))
            self.emit("console", f"[BLOCK] {exc}\n")
            raise

    def read_text(self, path: str) -> str:
        if not os.path.isfile(path):
            raise PolicyError("目标不是普通文件。")
        with open(path, "rb") as handle:
            raw = handle.read(self.LIMIT + 1)
        if len(raw) > self.LIMIT:
            raise PolicyError("文件超过 1 MiB。")
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PolicyError("文件不是 UTF-8 文本。") from exc

    def file_operation(self, op: Operation) -> dict:
        path = op.arguments["path"]
        if op.tool == "read_file":
            content = self.read_text(path)
            self.record(path, "读取", content)
            return {"path": path, "content": content}
        if op.tool == "list_files":
            entries = sorted(os.listdir(path))
            self.record(path, "列目录", "\n".join(entries[:1000]))
            return {"path": path, "entries": entries[:1000], "truncated": len(entries) > 1000}
        if op.tool == "delete_file":
            if not os.path.isfile(path):
                raise PolicyError("仅允许删除单个普通文件。")
            preview = diff_preview(path, None)
            os.unlink(path)
            self.record(path, "删除", preview)
            return {"deleted": path}
        if os.path.exists(path) and not os.path.isfile(path):
            raise PolicyError("写入目标不是普通文件。")
        # Replacing a hardlink with a new inode avoids modifying its other names.
        existed = os.path.exists(path)
        preview = diff_preview(path, op.arguments["content"])
        fd, temp = tempfile.mkstemp(prefix=".clawx-", suffix=".tmp", dir=os.path.dirname(path))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(op.arguments["content"])
                handle.flush()
                try:
                    os.fsync(handle.fileno())
                except OSError:
                    # Some virtual or network filesystems do not support fsync.
                    pass
            self.scope.resolve(temp)
            self.scope.resolve(path)
            os.replace(temp, path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
        self.record(path, "修改" if existed else "新建", preview)
        return {"written": path, "bytes": len(op.arguments["content"].encode("utf-8"))}

    def start_command(self, args: dict) -> subprocess.Popen:
        command, cwd = args["command"], args["cwd"]
        portable = self.portable_mode()
        if not portable:
            if os.name == "nt":
                shell = os.path.join(os.environ.get("SystemRoot", "C:\\Windows"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
                utf8 = "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); $OutputEncoding = [Console]::OutputEncoding; "
                argv = [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", utf8 + command]
            else:
                argv = ["/bin/sh", "-c", command]
        else:
            action = self.scoped_command(command, cwd)
            argv = [sys.executable, "-I", "-X", "utf8", "-u", "-c", COMMAND_HELPER, json.dumps(action)]
        self.emit("console", f"\n$ {command}\n[cwd] {cwd}\n[RUNNING]\n")
        kwargs = {"cwd": cwd, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
                  "stdin": subprocess.DEVNULL, "env": {**os.environ, "PYTHONIOENCODING": "utf-8"}}
        if os.name == "nt":
            kwargs.update(creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
        else:
            kwargs.update(start_new_session=True)
        process = subprocess.Popen(argv, **kwargs)
        process.clawx_encoding = "utf-8" if portable or os.name == "nt" else locale.getpreferredencoding(False)
        return process

    @staticmethod
    def terminate(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            taskkill = os.path.join(os.environ.get("SystemRoot", "C:\\Windows"), "System32", "taskkill.exe")
            try:
                subprocess.run([taskkill, "/PID", str(process.pid), "/T", "/F"],
                               capture_output=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                return
        if process.poll() is None:
            process.kill()

    def wait_command(self, process, args: dict, revision: int) -> dict:
        chunks: queue.Queue = queue.Queue(maxsize=128)
        reader_stop = threading.Event()

        def read_pipe(pipe, label):
            try:
                while not reader_stop.is_set():
                    data = os.read(pipe.fileno(), 4096)
                    if not data:
                        break
                    while not reader_stop.is_set():
                        try:
                            chunks.put((label, data), timeout=0.1)
                            break
                        except queue.Full:
                            continue
            except (OSError, ValueError):
                return
            finally:
                pipe.close()

        readers = [threading.Thread(target=read_pipe, args=(pipe, label), daemon=True)
                   for pipe, label in ((process.stdout, "stdout"), (process.stderr, "stderr"))]
        for reader in readers:
            reader.start()
        started, total = time.monotonic(), 0
        output, reason, reason_since = [], "", 0.0
        encoding = process.clawx_encoding
        decoders = {name: codecs.getincrementaldecoder(encoding)(errors="replace") for name in ("stdout", "stderr")}
        try:
            while process.poll() is None or any(r.is_alive() for r in readers) or not chunks.empty():
                if reason and time.monotonic() - reason_since > 5:
                    break
                if not reason:
                    if self.cancel.is_set():
                        reason = "用户停止"
                    elif self.state.snapshot()["revision"] != revision:
                        reason = "安全设置改变"
                    elif time.monotonic() - started > 60:
                        reason = "超过 60 秒时限"
                    elif total > 512 * 1024:
                        reason = "输出超过 512 KiB"
                    if reason:
                        reason_since = time.monotonic()
                        self.terminate(process)
                try:
                    label, raw = chunks.get(timeout=0.05)
                    total += len(raw)
                    if total <= 512 * 1024:
                        text = decoders[label].decode(raw)
                        self.emit("console", f"[{label}] {text}")
                        output.append(f"[{label}] {text}")
                except queue.Empty:
                    continue
            code = process.wait(timeout=5)
            for label, decoder in decoders.items():
                tail = decoder.decode(b"", final=True)
                if tail:
                    self.emit("console", f"[{label}] {tail}")
                    output.append(f"[{label}] {tail}")
        finally:
            reader_stop.set()
            if process.poll() is None:
                self.terminate(process)
            for reader in readers:
                reader.join(timeout=0.2)
        self.emit("console", f"\n[EXIT {code}]" + (f" {reason}" if reason else "") + "\n")
        if self.cancel.is_set():
            raise Cancelled("命令已停止")
        return {"command": args["command"], "cwd": args["cwd"], "returncode": code,
                "output": "".join(output)[-24000:], "terminated": reason}
