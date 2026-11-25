"""Test double for both upstreams LiteLLM talks to (stdlib only).

- Corporate LiteLLM (OpenAI API): GET /v1/models, POST /chat/completions, /v1/chat/completions
- Bedrock runtime (Converse API):  POST /model/{modelId}/converse
- Test controls: POST /_fail_bedrock {"on": bool}, GET /_requests, DELETE /_requests
- With --dns-ip: a DNS server on UDP 53 that records every name looked up and answers A
  queries with --dns-ip, plus a tarpit on TCP 80/443 at that address that records each
  connection and never replies. GET /_outbound -> {"dns": [...], "tarpit": [...]}.
  (Needs root for the low ports; the offline test runs the mock as root.)

Every upstream request is recorded as {"path", "authorization"} (path URL-decoded).
"""
import argparse
import json
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_lock = threading.Lock()
_requests = []
_fail_bedrock = False
_dns_queries = []
_tarpit_connections = []


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep test output readable
        pass

    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw) if raw else {}

    def _record(self, path):
        with _lock:
            _requests.append({"path": path, "authorization": self.headers.get("Authorization")})

    def do_GET(self):
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        if path == "/_requests":
            with _lock:
                return self._send(200, list(_requests))
        if path == "/_outbound":
            with _lock:
                return self._send(200, {"dns": sorted(set(_dns_queries)), "tarpit": list(_tarpit_connections)})
        if path in ("/v1/models", "/models"):
            self._record(path)
            return self._send(200, {"object": "list", "data": [{"id": "mock-model", "object": "model"}]})
        self._send(404, {"error": f"no route {path}"})

    def do_DELETE(self):
        if self.path == "/_requests":
            with _lock:
                _requests.clear()
            return self._send(200, {"cleared": True})
        self._send(404, {"error": "no route"})

    def do_POST(self):
        global _fail_bedrock
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        body = self._body()
        if path == "/_fail_bedrock":
            _fail_bedrock = bool(body.get("on"))
            return self._send(200, {"fail_bedrock": _fail_bedrock})
        if path in ("/chat/completions", "/v1/chat/completions"):
            self._record(path)
            return self._send(200, {
                "id": "chatcmpl-mock", "object": "chat.completion", "created": int(time.time()),
                "model": body.get("model", "mock-model"),
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "pong from corp"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
            })
        if path.startswith("/model/") and path.endswith("/converse"):
            self._record(path)
            if _fail_bedrock:
                return self._send(500, {"message": "mock bedrock failure"})
            return self._send(200, {
                "output": {"message": {"role": "assistant", "content": [{"text": "pong from bedrock"}]}},
                "stopReason": "end_turn",
                "usage": {"inputTokens": 5, "outputTokens": 3, "totalTokens": 8},
                "metrics": {"latencyMs": 1},
            })
        self._send(404, {"error": f"no route {path}"})


def _dns_name(packet):
    labels, i = [], 12
    while packet[i]:
        labels.append(packet[i + 1:i + 1 + packet[i]].decode("ascii", "replace"))
        i += 1 + packet[i]
    return ".".join(labels), i + 1  # index just after the name


def serve_dns(answer_ip):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 53))
    while True:
        packet, addr = sock.recvfrom(512)
        try:
            name, end = _dns_name(packet)
        except IndexError:
            continue
        qtype = int.from_bytes(packet[end:end + 2], "big")
        with _lock:
            _dns_queries.append(name)
        question = packet[12:end + 4]
        if qtype == 1:  # A: point every name at the tarpit
            answer = (b"\xc0\x0c\x00\x01\x00\x01" + (5).to_bytes(4, "big") + b"\x00\x04"
                      + socket.inet_aton(answer_ip))
            header = packet[:2] + b"\x81\x80\x00\x01\x00\x01\x00\x00\x00\x00"
        else:  # anything else: no records
            answer = b""
            header = packet[:2] + b"\x81\x80\x00\x01\x00\x00\x00\x00\x00\x00"
        sock.sendto(header + question + answer, addr)


def serve_tarpit(port):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", port))
    server.listen(64)
    held = []
    while True:
        conn, addr = server.accept()
        held.append(conn)  # accept, never reply: like a firewall that drops packets
        with _lock:
            _tarpit_connections.append(f"{addr[0]} -> :{port}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--dns-ip", help="run the DNS recorder and tarpit, answering with this IP")
    args = parser.parse_args()
    if args.dns_ip:
        threading.Thread(target=serve_dns, args=(args.dns_ip,), daemon=True).start()
        for port in (80, 443):
            threading.Thread(target=serve_tarpit, args=(port,), daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
