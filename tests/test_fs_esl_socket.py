#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""fs_esl_socket 单元测试：用本地假 FS 服务端验证 ESL 协议实现。

覆盖设计文档 1.3 的 7 大坑：
- auth 握手读响应判定 +OK（坑 4）
- api 响应 Content-Type: api/response + body（坑 6）
- plain header 值 URL 编码，getHeader 必须 unquote（坑 1）
- header 折叠续行拼回（坑 2）
- Content-Length 分帧（坑 3）
- recvEvent 可超时返回 None（坑 5）
"""

import os
import queue
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fs_esl_socket import ESLConnection  # noqa: E402


class FakeFS:
    """扮演 FreeSWITCH ESL 服务端：自动完成 auth 握手，之后按响应队列回包。"""

    def __init__(self, accept_auth=True):
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.conn = None
        self.commands = []  # 收到的命令（去掉 \\n\\n）
        self._accept_auth = accept_auth
        self._responses = queue.Queue()  # 命令响应（api/event 的回复）
        self._events = queue.Queue()  # 服务端主动推送的事件（recvEvent 消费）
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._serve, daemon=True)
        self._t.start()

    def _serve(self):
        try:
            self.conn, _ = self.srv.accept()
        except OSError:
            return
        self.conn.settimeout(0.2)
        # 握手第一步：服务端先发 auth/request
        try:
            self.conn.sendall(b"Content-Type: auth/request\n\n")
        except OSError:
            return
        buf = b""
        authed = False
        while not self._stop.is_set():
            # 主动推送待发事件（recvEvent 消费）
            try:
                data = self._events.get_nowait()
                if data:
                    self.conn.sendall(data)
            except queue.Empty:
                pass
            except OSError:
                return
            try:
                chunk = self.conn.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n\n" in buf:
                cmd, buf = buf.split(b"\n\n", 1)
                self.commands.append(cmd)
                if not authed:
                    authed = True
                    if self._accept_auth and cmd.startswith(b"auth"):
                        self.conn.sendall(
                            b"Content-Type: command/reply\nReply-Text: +OK accepted\n\n"
                        )
                    else:
                        self.conn.sendall(
                            b"Content-Type: command/reply\nReply-Text: -ERR invalid\n\n"
                        )
                    continue
                # 后续命令：从响应队列取一个响应发送
                try:
                    data = self._responses.get(timeout=5)
                except queue.Empty:
                    data = b""
                if data:
                    try:
                        self.conn.sendall(data)
                    except OSError:
                        return

    def send_raw_event(self, data):
        """直接推送原始字节帧（用于构造折叠续行等特殊场景）。"""
        self._events.put(data)

    def queue_response(self, data):
        self._responses.put(data)

    def send_event(self, headers, body=""):
        """发送真实 ESL 事件帧：外层 header(Content-Type: text/event-plain) + body 是字段行。"""
        lines = []
        for k, v in headers:
            lines.append("%s: %s" % (k, v))
        event_body = "\n".join(lines) + "\n"
        self.send_event_raw(event_body)

    def send_event_raw(self, event_body):
        """发送给定事件 body 的完整帧（自动算 Content-Length）。"""
        frame = (
            "Content-Type: text/event-plain\n"
            "Content-Length: %d\n\n" % len(event_body.encode("utf-8"))
        ) + event_body
        self._events.put(frame.encode("utf-8"))

    def send_api_response(self, body):
        data = body.encode("utf-8")
        self.queue_response(
            b"Content-Type: api/response\nContent-Length: "
            + str(len(data)).encode()
            + b"\n\n"
            + data
        )

    def reply_ok(self):
        self.queue_response(
            b"Content-Type: command/reply\nReply-Text: +OK accepted\n\n"
        )

    def close(self):
        self._stop.set()
        for s in (self.conn, self.srv):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass


class BaseFS(unittest.TestCase):
    def setUp(self):
        self.fs = FakeFS()
        self.con = ESLConnection("127.0.0.1", self.fs.port, "pw", timeout=2.0)

    def tearDown(self):
        try:
            self.con.disconnect()
        except Exception:
            pass
        self.fs.close()


class TestAuthHandshake(BaseFS):
    def test_auth_ok(self):
        self.assertTrue(self.con.connected())
        # 收到的第一条命令应是 auth pw
        self.assertTrue(self.fs.commands and self.fs.commands[0].startswith(b"auth "))

    def test_auth_rejected(self):
        self.con.disconnect()
        self.fs.close()
        fs = FakeFS(accept_auth=False)
        con = ESLConnection("127.0.0.1", fs.port, "pw", timeout=2.0)
        self.assertFalse(con.connected())
        con.disconnect()
        fs.close()

    def test_connect_refused(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        con = ESLConnection("127.0.0.1", port, "pw", timeout=1.0)
        self.assertFalse(con.connected())
        con.disconnect()


class TestApi(BaseFS):
    def test_api_response_body(self):
        self.fs.send_api_response("2 total\n")
        ev = self.con.api("show calls count")
        self.assertIsNotNone(ev)
        self.assertEqual(ev.getBody(), "2 total\n")
        self.assertIn(b"api show calls count", self.fs.commands[-1])


class TestEventsSubscribe(BaseFS):
    def test_events_sends_subscribe(self):
        self.fs.reply_ok()
        ok = self.con.events("plain", "CHANNEL_CREATE CHANNEL_HANGUP_COMPLETE")
        self.assertTrue(ok)
        self.assertTrue(
            any(c.startswith(b"event plain") for c in self.fs.commands)
        )


class TestEventParsing(BaseFS):
    def test_getheader_unquote(self):
        self.fs.send_event(
            [
                ("Event-Name", "CHANNEL_CREATE"),
                ("variable_sip_full_via", "SIP/2.0/UDP%201.2.3.4%3A5060"),
            ]
        )
        ev = self.con.recvEvent(timeout=2.0)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.getHeader("Event-Name"), "CHANNEL_CREATE")
        self.assertEqual(
            ev.getHeader("variable_sip_full_via"), "SIP/2.0/UDP 1.2.3.4:5060"
        )
        self.assertIsNone(ev.getHeader("not-exist"))

    def test_folded_header(self):
        # 折叠续行：body 里的字段值下一行以空格/TAB 开头
        self.fs.send_event_raw(
            "Event-Name: CHANNEL_CREATE\nvariable_sdp: v=0\n o=alice\n"
        )
        ev = self.con.recvEvent(timeout=2.0)
        self.assertIsNotNone(ev)
        # 续行去掉前导空白直接拼回
        self.assertEqual(ev.getHeader("variable_sdp"), "v=0o=alice")
        self.assertEqual(ev.getHeader("Event-Name"), "CHANNEL_CREATE")

    def test_content_length_body(self):
        # 事件字段在 body 里（真实 ESL 结构），getBody() 对事件应置空
        self.fs.send_event(
            [("Event-Name", "CHANNEL_HANGUP_COMPLETE"), ("Unique-ID", "abc")]
        )
        ev = self.con.recvEvent(timeout=2.0)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.getHeader("Unique-ID"), "abc")
        self.assertEqual(ev.getHeader("Event-Name"), "CHANNEL_HANGUP_COMPLETE")
        self.assertEqual(ev.getBody(), "")  # 事件无独立 body
        names = set(ev.getHeaderNames())
        self.assertIn("Event-Name", names)
        self.assertIn("Unique-ID", names)

    def test_recv_timeout_returns_none(self):
        t0 = time.monotonic()
        ev = self.con.recvEvent(timeout=0.5)
        self.assertIsNone(ev)
        self.assertLess(time.monotonic() - t0, 2.0)

    def test_recv_returns_none_on_disconnect(self):
        self.fs.close()  # 服务端断开
        ev = self.con.recvEvent(timeout=3.0)
        self.assertIsNone(ev)
        self.assertFalse(self.con.connected())


class TestDisconnect(BaseFS):
    def test_disconnect_idempotent(self):
        self.con.disconnect()
        self.assertFalse(self.con.connected())
        self.con.disconnect()  # 幂等，不抛异常


if __name__ == "__main__":
    unittest.main()
