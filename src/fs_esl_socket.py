#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""纯 socket FreeSWITCH ESL 客户端（替换 python-esl 绑定）。

背景
----
网关容器镜像里 `from ESL import ESLconnection` 无法使用：镜像内置的
`_ESL.so` 由 Debian buster python3.7 编译、链接 `libpython3.7m.so.1.0`，
python3.11/3.12 均 ABI 不匹配（py3.12 还缺 `imp`）。而 `esl_client.py:21`
是裸顶层 import、`main.py:16` 顶层依赖它，导致容器内网关**启动即崩**。

本模块用纯标准库（socket / threading / urllib.parse）实现 ESL 协议的最小
子集，签名与 python-esl 对齐，业务代码只需改 2 行 import：

    from ESL import ESLconnection
    → from fs_esl_socket import ESLConnection as ESLconnection

协议事实（FS 1.11.2 实测，见 docs/design 第一部分）：
- ESL 消息是「外层 header + body」两段：外层 header 只有 Content-Length /
  Content-Type；真正的事件字段（Event-Name / Unique-ID / variable_*）在 **body** 里
  （Content-Type: text/event-plain 时，body 是一组 `Name: value` 行）。
- plain 事件字段的值经 URL 编码（空格→%20 等），getHeader() 必须
  `urllib.parse.unquote`，否则静默脏数据。
- 长字段值会折叠：续行以空格/TAB 开头，须先拼回再 unquote。
- 帧以 header 块里的 Content-Length 分界，不能按行读。
- auth 握手须读响应判定 `+OK`，不可 sleep。
- recvEvent() 可超时返回 None；连接断开也返回 None（与现有重连逻辑一致）。

约束
----
- 纯标准库，不引入第三方依赖。
- 所有 I/O 带超时，绝不无限阻塞（连接对象单线程使用，与现状一致）。
- getHeader() 内部 unquote 并缓存结果。
"""

import logging
import socket
import threading
import time
from urllib.parse import unquote

log = logging.getLogger("fs_esl_socket")

# 阻塞模式 recvEvent 的空闲探活间隔（秒）。连续空闲达到该阈值时发一次
# `api status` 探活，失败则判定断线返回 None，成功则重置计时继续等。
_IDLE_PROBE_INTERVAL = 60.0


class ESLEvent:
    """一条已解析的 ESL 事件（headers + 可选 body）。"""

    __slots__ = ("_headers", "_body", "_decoded")

    def __init__(self, headers, body=""):
        # headers: {name: raw_value}，raw_value 为编码后原值（plain 模式经 URL 编码）。
        self._headers = headers
        self._body = body
        self._decoded = {}  # name -> unquote 后的值缓存

    def getHeader(self, name):
        """返回 header 值（已 unquote）。缺失返回 None。"""
        if name not in self._headers:
            return None
        val = self._decoded.get(name)
        if val is None:
            val = unquote(self._headers[name])
            self._decoded[name] = val
        return val

    def getHeaderNames(self):
        """返回全部 header 名（保持接收顺序）。"""
        return list(self._headers.keys())

    def getBody(self):
        """返回 body 文本（可能为空串）。"""
        return self._body

    def serialize(self, fmt="plain"):
        """调试用：把事件还原为文本。"""
        out = []
        for k, v in self._headers.items():
            out.append("%s: %s" % (k, v))
        out.append("")
        if self._body:
            out.append(self._body)
        return "\n".join(out)

    def __repr__(self):
        return "<ESLEvent headers=%d body=%d>" % (len(self._headers), len(self._body))


class ESLConnection:
    """TCP 8021 ESL 连接。签名对齐 python-esl 的 ESLconnection。"""

    def __init__(self, host, port, password, timeout=3.0, stop_event=None):
        self._host = host
        self._port = int(port)
        self._password = str(password)
        self._timeout = timeout
        self._stop_event = stop_event  # 可选 threading.Event，set 后 recvEvent 返回 None
        self._sock = None
        self._connected = False
        self._buf = b""
        self._idle = 0
        self._lock = threading.Lock()  # 保护单连接上的收发，避免并发交错
        self._connect_and_auth()

    # ---------- 连接管理 ----------

    def _connect_and_auth(self):
        try:
            sock = socket.create_connection((self._host, self._port), timeout=self._timeout)
        except OSError as e:
            log.warning("ESL connect %s:%s failed: %s", self._host, self._port, e)
            self._connected = False
            return
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        self._sock = sock
        try:
            # 1) 服务端先发 auth/request
            self._sock.settimeout(self._timeout)
            self._read_frame_blocking(self._timeout)
            # 2) 回 auth <password>
            self._sendall(("auth %s\n\n" % self._password).encode("utf-8"))
            # 3) 读 command/reply，判定 +OK
            headers, _ = self._read_frame_blocking(self._timeout)
            reply = headers.get("Reply-Text", "") or ""
            self._connected = reply.startswith("+OK")
            if not self._connected:
                log.warning("ESL auth rejected: %r", reply)
        except (EOFError, OSError, TimeoutError) as e:
            log.warning("ESL auth handshake failed: %s", e)
            self._connected = False

    def connected(self):
        return self._connected

    def disconnect(self):
        with self._lock:
            self._connected = False
            sock, self._sock = self._sock, None
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    # ---------- 命令 ----------

    def events(self, fmt, event_list):
        """订阅事件：发送 `event <fmt> <list>` 并消费其 reply。"""
        with self._lock:
            try:
                self._sendall(("event %s %s\n\n" % (fmt, event_list)).encode("utf-8"))
                self._read_frame_blocking(self._timeout)
                return True
            except (EOFError, OSError, TimeoutError) as e:
                log.warning("ESL events subscribe failed: %s", e)
                self._connected = False
                return False

    def api(self, cmd, timeout=10.0):
        """发送 `api <cmd>`，返回 ESLEvent（body 为命令输出）；失败返回 None。"""
        with self._lock:
            try:
                self._sendall(("api %s\n\n" % cmd).encode("utf-8"))
                headers, body = self._read_frame_blocking(timeout)
                return ESLEvent(headers, body)
            except (EOFError, OSError, TimeoutError) as e:
                log.warning("ESL api(%s) failed: %s", cmd, e)
                self._connected = False
                return None

    def bgapi(self, cmd, timeout=10.0):
        """发送 `bgapi <cmd>`（后台 api），返回 ESLEvent；失败返回 None。"""
        with self._lock:
            try:
                self._sendall(("bgapi %s\n\n" % cmd).encode("utf-8"))
                headers, body = self._read_frame_blocking(timeout)
                return ESLEvent(headers, body)
            except (EOFError, OSError, TimeoutError) as e:
                log.warning("ESL bgapi(%s) failed: %s", cmd, e)
                self._connected = False
                return None

    # ---------- 事件读取 ----------

    def recvEvent(self, timeout=None):
        """读下一条事件。

        - timeout=None：阻塞直到收到事件或断线；空闲约 60s 发 `api status`
          探活，探活失败判为断线。断线返回 None（与现有重连逻辑兼容）。
        - timeout=N：最多等 N 秒，超时返回 None（用于测试/可中断等待）。
        """
        if self._stop_event is not None and self._stop_event.is_set():
            return None
        wait = 1.0 if timeout is None else max(0.05, min(timeout, 1.0))
        deadline = None if timeout is None else time.monotonic() + timeout

        with self._lock:
            lines = []
            self._sock.settimeout(wait)
            while True:
                try:
                    line = self._readline()
                except socket.timeout:
                    if self._stop_event is not None and self._stop_event.is_set():
                        return None
                    if deadline is not None and time.monotonic() >= deadline:
                        return None
                    if lines:
                        # 帧读了一半超时：已读行保留在 lines，继续等，不丢数据。
                        continue
                    # 帧间空闲：阻塞模式下累计探活。
                    self._idle += 1
                    if timeout is None and self._idle >= _IDLE_PROBE_INTERVAL:
                        self._idle = 0
                        if not self._probe():
                            self._connected = False
                            return None
                    continue
                except (EOFError, OSError):
                    self._connected = False
                    return None
                if line == "":
                    break
                lines.append(line)

            headers = self._parse_header_lines(lines)
            cl = int(headers.get("Content-Length", "0") or "0")
            body = ""
            if cl > 0:
                try:
                    body = self._read_exact(cl).decode("utf-8", "replace")
                except (EOFError, OSError):
                    self._connected = False
                    return None
            self._idle = 0
            return self._build_event(headers, body)

    @staticmethod
    def _build_event(outer_headers, body):
        """根据 Content-Type 组装 ESLEvent。

        - text/event-plain：事件字段在 body 里（Name: value 行），解析为 headers，
          body 置空（与 python-esl 语义一致）。
        - 其他（api/response、command/reply）：headers = 外层 header，body = 输出。
        """
        content_type = (outer_headers.get("Content-Type") or "").lower()
        if content_type == "text/event-plain":
            return ESLEvent(ESLConnection._parse_event_body(body), "")
        return ESLEvent(outer_headers, body)

    @staticmethod
    def _parse_event_body(body):
        """解析 text/event-plain 的 body 为事件字段（Name: value 行）。

        处理折叠续行（空格/TAB 开头拼回上一行）。值保持编码态，getHeader 时 unquote。
        """
        fields = {}
        current = None
        for line in body.split("\n"):
            if line == "":
                continue
            if line[:1] in (" ", "\t") and current is not None:
                fields[current] += line.lstrip()
                continue
            if ":" in line:
                name, _, value = line.partition(":")
                name = name.strip()
                value = value.strip()
                fields[name] = value
                current = name
            else:
                current = None
        return fields

    def _probe(self):
        """空闲探活：发 api status，成功返回 True。"""
        try:
            ev = self.api("status")
            return ev is not None
        except Exception:
            return False

    # ---------- 底层 I/O ----------

    def _sendall(self, data):
        self._sock.sendall(data)

    def _readline(self):
        """从缓冲读一行（去掉 \\r\\n）。无数据超时抛 socket.timeout；断开抛 EOFError。"""
        while True:
            nl = self._buf.find(b"\n")
            if nl >= 0:
                line = self._buf[:nl]
                self._buf = self._buf[nl + 1:]
                return line.rstrip(b"\r").decode("utf-8", "replace")
            try:
                chunk = self._sock.recv(4096)
            except socket.timeout:
                raise
            except OSError as e:
                raise EOFError("connection closed: %s" % e)
            if not chunk:
                raise EOFError("connection closed by peer")
            self._buf += chunk

    def _read_exact(self, n):
        """读恰好 n 字节；断线抛 EOFError；超时重试（不丢已读）。"""
        data = b""
        while len(data) < n:
            if self._buf:
                take = min(n - len(data), len(self._buf))
                data += self._buf[:take]
                self._buf = self._buf[take:]
                continue
            try:
                chunk = self._sock.recv(n - len(data))
            except socket.timeout:
                continue
            except OSError as e:
                raise EOFError(str(e))
            if not chunk:
                raise EOFError("connection closed by peer")
            data += chunk
        return data

    def _read_frame_blocking(self, timeout):
        """读一个完整帧（header 块 + Content-Length body）。超时抛 TimeoutError；断线抛 EOFError。"""
        self._sock.settimeout(timeout)
        deadline = time.monotonic() + timeout
        lines = []
        while True:
            try:
                line = self._readline()
            except socket.timeout:
                if time.monotonic() >= deadline:
                    raise TimeoutError("ESL frame read timeout")
                continue
            except EOFError:
                raise
            if line == "":
                break
            lines.append(line)
        headers = self._parse_header_lines(lines)
        cl = int(headers.get("Content-Length", "0") or "0")
        body = ""
        if cl > 0:
            body = self._read_exact(cl).decode("utf-8", "replace")
        return headers, body

    @staticmethod
    def _parse_header_lines(lines):
        """解析 header 行列表，处理折叠续行（空格/TAB 开头拼回上一行）。"""
        headers = {}
        current = None
        for line in lines:
            if line[:1] in (" ", "\t") and current is not None:
                # 折叠续行：去掉前导空白后直接拼到上一 header 值末尾。
                headers[current] += line.lstrip()
                continue
            if ":" in line:
                name, _, value = line.partition(":")
                name = name.strip()
                value = value.strip()
                headers[name] = value
                current = name
            else:
                current = None
        return headers


# 兼容别名：某些调用点可能直接 import ESLconnection 这个名字。
ESLconnection = ESLConnection
