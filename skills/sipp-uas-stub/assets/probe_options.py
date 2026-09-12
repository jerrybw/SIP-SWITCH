#!/usr/bin/env python3
"""Raw UDP OPTIONS probe -- sends one OPTIONS and prints whatever comes back.

Why: proves the stub answers OPTIONS without depending on any SIP library,
so you cannot blame "the probe's own SIP stack is buggy".

Usage:
    python3 probe_options.py <host> [port] [timeout]

In a containerized setup run it on the same docker network:
    docker run --rm --network <compose>_sipnet --entrypoint python3 \
        -v <skill>/assets:/p <image-with-python> /p/probe_options.py <stub-host> 5060

Exit codes: 0 = got 200 OK | 1 = timeout (stub dropped it) | 2 = non-200 reply
"""
import socket
import sys
import uuid

HOST = sys.argv[1] if len(sys.argv) > 1 else "sipp-reg"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 5060
TIMEOUT = float(sys.argv[3]) if len(sys.argv) > 3 else 6.0

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(("", 0))
s.settimeout(TIMEOUT)
local_ip, local_port = s.getsockname()
tag = "probe" + uuid.uuid4().hex[:8]
branch = "z9hG4bK" + uuid.uuid4().hex[:12]
callid = "optprobe-%s@probe" % uuid.uuid4().hex[:8]

# NOTE: keep the local IP that the socket actually bound to. Sending from 127.0.0.1 on a
# container network makes the reply unroutable and looks like "the stub is not answering".
msg = (
    "OPTIONS sip:%s:%d SIP/2.0\r\n"
    "Via: SIP/2.0/UDP %s:%d;branch=%s;rport\r\n"
    "Max-Forwards: 70\r\n"
    "From: <sip:probe@probe>;tag=%s\r\n"
    "To: <sip:%s:%d>\r\n"
    "Call-ID: %s\r\n"
    "CSeq: 1 OPTIONS\r\n"
    "Contact: <sip:probe@%s:%d>\r\n"
    "User-Agent: raw-probe\r\n"
    "Accept: application/sdp\r\n"
    "Content-Length: 0\r\n"
    "\r\n"
) % (HOST, PORT, local_ip, local_port, branch, tag, HOST, PORT, callid, local_ip, local_port)

print("[probe] local=%s:%d  ->  %s:%d" % (local_ip, local_port, HOST, PORT))
print("[probe] >>>\n" + msg.split("\r\n\r\n")[0])
s.sendto(msg.encode(), (HOST, PORT))

try:
    data, addr = s.recvfrom(65535)
    print("[probe] <<< from %s:%d (%d bytes)" % (addr[0], addr[1], len(data)))
    print(data.decode(errors="replace"))
    first = data.decode(errors="replace").split("\r\n")[0]
    if first.startswith("SIP/2.0 200"):
        print("[probe] RESULT: OK -- stub answered OPTIONS with 200")
        sys.exit(0)
    print("[probe] RESULT: non-200 reply: %s" % first)
    sys.exit(2)
except socket.timeout:
    print("[probe] RESULT: TIMEOUT -- no reply in %.0fs (stub dropped OPTIONS)" % TIMEOUT)
    sys.exit(1)
