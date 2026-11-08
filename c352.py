"""352 X83 air purifier cloud client.

Protocol reverse-engineered from the 352life Android app
(com.bugull.a352airclearner) for personal, non-commercial home automation.

Two channels are used:
  * HTTPS  app.352air.com/api/v1/*   login / token refresh
  * TLS    wifi.352air.com:11591     a custom binary protocol for control
"""
import hashlib
import json
import os
import socket
import ssl
import threading
import time

import requests

# Application id embedded in the vendor app, only used to sign API requests.
APPID = "8d5018f2bc0f11ea8e6388e9fe5ac5b6"
BASE = "https://app.352air.com"
BALANCE_HOST = "wifi.352air.com"
BALANCE_PORT = 11591

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

# UI gear index -> protocol gear value (1..6)
GEARS = [1, 2, 3, 4, 5, 6]
# UI mode index -> protocol mode value (1=auto, 2=sleep, 3=manual, 5=turbo)
MODES = [1, 2, 3, 5]
VAL_ON, VAL_OFF = 0x35, 0x11
JOIN_CODE = bytes([0x33, 0x35, 0x32, 0x1C, 0x94, 0x4E])


def _sign(path, ts):
    return hashlib.md5(f"{APPID}{path}{ts}".encode()).hexdigest()


class Api:
    """Thin client for the JSON API. ts is in seconds; sign = md5(appid+path+ts)."""

    def __init__(self, token=None):
        self.token = token
        self._session = requests.Session()
        self._session.trust_env = False   # ignore system proxy settings

    def _call(self, method, path, body=None):
        ts = str(int(time.time()))
        headers = {
            "Content-Type": "application/json;charset=utf-8",
            "ts": ts,
            "sign": _sign(path, ts),
        }
        if self.token:
            headers["Authorization"] = f"Token {self.token}"
        r = self._session.request(method, BASE + path, json=body, headers=headers, timeout=20)
        r.raise_for_status()
        return r.json()

    def send_code(self, account):
        """SMS login code. action=2 means 'login by code'."""
        return self._call("POST", "/api/v1/enduser/send_code",
                          {"account": account, "action": 2, "area": "86"})

    def login(self, account, code):
        return self._call("POST", "/api/v1/enduser/login",
                          {"account": account, "code": code, "area": "86", "reason": "login"})

    def refresh(self, refresh_token):
        return self._call("POST", "/api/v1/enduser/refresh_token",
                          {"refresh_token": refresh_token})


def _ssl_sock(host, port, timeout=20):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    s = socket.create_connection((host, port), timeout=timeout)
    return ctx.wrap_socket(s)


def frame(payload, mac=0, company=0, dtype=0, auth=0, idx=0, dev=False):
    """16 byte header + payload, mirrors ProtocolHeaderBuilder.assembleCommand."""
    h = bytearray(16)
    h[0] = 0xA1
    h[1] = 4 if dev else 0
    h[2:8] = mac.to_bytes(6, "big")
    h[8] = (len(payload) + 7) & 0xFF
    h[9] = 0
    h[10:12] = idx.to_bytes(2, "big")
    h[12] = company
    h[13] = dtype
    h[14:16] = auth.to_bytes(2, "big")
    return bytes(h) + payload


def _serial(*vals):
    """NetUtil.buildSerialData: payload bytes followed by a sum byte."""
    data = bytes(vals)
    return data + bytes([sum(data) & 0xFF])


def read_frame(sock):
    """Read one frame; byte 8 holds (payload length + 7)."""
    head = b""
    while len(head) < 9:
        chunk = sock.recv(9 - len(head))
        if not chunk:
            raise ConnectionError("closed")
        head += chunk
    n = head[8]
    body = b""
    while len(body) < n:
        chunk = sock.recv(n - len(body))
        if not chunk:
            raise ConnectionError("closed")
        body += chunk
    return head + body


class ProtocolError(Exception):
    pass


class Device:
    def __init__(self, mac, auth_code, company_code=0xA1, device_type=0x02):
        self.mac = mac
        self.auth = auth_code
        self.company = company_code
        self.dtype = device_type
        self.idx = 0
        # last known state
        self.on = None
        self.gear = None
        self.mode = None
        self.pm25 = None
        self.online = False
        self.last_state_ts = 0.0


class Tcp352:
    """Balance server -> work server -> join -> subscribe -> control."""

    def __init__(self, token, device):
        self.token = token
        self.dev = device
        self.sock = None
        self._hb_stop = threading.Event()
        self._hb = None
        self.lock = threading.Lock()
        self.got_state = threading.Event()
        self._gidx = 0

    def _next_idx(self):
        with self.lock:
            self._gidx = (self._gidx + 1) % 32768
            return self._gidx

    def connect(self, tries=3):
        for i in range(tries):
            try:
                return self._connect_once()
            except Exception:
                if i == tries - 1:
                    raise
                time.sleep(2 * (i + 1))

    def _connect_once(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        # 1. ask the balance server for a work server
        s = _ssl_sock(BALANCE_HOST, BALANCE_PORT)
        s.sendall(frame(bytes([0x81]), idx=self._next_idx()))
        f = read_frame(s)
        if f[16] != 0x81:
            s.close()
            raise ProtocolError(f"expect work-server response, cmd={f[16]:#x}")
        ip = socket.inet_ntoa(f[17:21])
        port = int.from_bytes(f[21:23], "big")
        s.close()
        # 2. join the work server with the air token
        s = _ssl_sock(ip, port)
        tok = self.token.encode()
        payload = bytes([0x82, len(JOIN_CODE)]) + JOIN_CODE + bytes([0xFF, len(tok)]) + tok
        s.sendall(frame(payload, idx=self._next_idx()))
        f = read_frame(s)
        if f[16] != 0x82 or f[17] != 0:
            s.close()
            raise ProtocolError(f"join refused: {f.hex()}")
        self.sock = s
        # 3. the app's join sequence; without 0x89/0x86 the device ignores control frames
        self._hb_stop.clear()
        self._hb = threading.Thread(target=self._heartbeat, daemon=True)
        self._hb.start()
        self._send_abnormal_login()
        self.subscribe()
        self.query_state()
        return True

    def _send_abnormal_login(self):
        h = bytearray(16)
        h[0] = 0xA1
        h[2:8] = (0xFFFFFFFFFFFF).to_bytes(6, "big")
        h[8] = 1 + 7
        h[10:12] = self._next_idx().to_bytes(2, "big")
        h[12] = 0xA1
        h[13] = 0xAA
        h[14:16] = bytes([0xCB, 0x76])
        pkt = bytes(h) + bytes([0x89])
        with self.lock:
            if self.sock:
                self.sock.sendall(pkt)

    def _heartbeat(self):
        my_sock = self.sock
        while not self._hb_stop.wait(5):
            try:
                pkt = frame(bytes([0x61]), idx=self._next_idx())
            except Exception:
                return
            if not self.lock.acquire(timeout=3):
                continue
            try:
                if self.sock is not my_sock or not self.sock:
                    return
                self.sock.sendall(pkt)
            except OSError:
                return
            finally:
                self.lock.release()

    def close(self):
        self._hb_stop.set()
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def _send_dev(self, payload):
        d = self.dev
        d.idx = (d.idx + 1) % 65536
        pkt = frame(bytes([0x01]) + payload, mac=d.mac, company=d.company,
                    dtype=d.dtype, auth=d.auth, idx=d.idx, dev=True)
        with self.lock:
            if not self.sock:
                raise ConnectionError("not connected")
            self.sock.sendall(pkt)

    def subscribe(self):
        d = self.dev
        pkt = frame(bytes([0x86, 0x01, 0x02, 0x00]), mac=d.mac, company=d.company,
                    dtype=d.dtype, auth=d.auth, idx=0, dev=True)
        with self.lock:
            if self.sock:
                self.sock.sendall(pkt)

    # -- controls --
    def set_power(self, on):
        """Not used by autopm: the automation deliberately never powers the device."""
        self._send_dev(_serial(0xA5, 0xA0, 0x5E, VAL_ON if on else VAL_OFF, 0x00))

    def set_speed(self, index):
        """index is 0-based: 0 -> gear 1 ... 5 -> gear 6 (max)."""
        self._send_dev(_serial(0xA5, 0xA0, 0x52, GEARS[index], 0x00))

    def set_mode(self, index):
        self._send_dev(_serial(0xA5, 0xA0, 0x51, MODES[index], 0x00))

    def query_state(self):
        self._send_dev(bytes([0xA5, 0xA0, 0x11, 0x11, 0x00, 0x00]))

    # -- receive loop --
    def recv_loop(self, on_state=None):
        """Blocking; parses frames, updates device state, reconnects on error."""
        while True:
            try:
                if self.sock is None:
                    raise ConnectionError("not connected")
                self.sock.settimeout(25)
                f = read_frame(self.sock)
            except socket.timeout:
                continue          # idle socket is normal, no event traffic
            except (OSError, ConnectionError):
                try:
                    self.connect()
                except Exception as e:
                    print(f"[tcp] reconnect failed: {e}")
                    time.sleep(15)
                continue
            self._handle(f, on_state)

    def _handle(self, f, on_state):
        cmd = f[16] if len(f) > 16 else None
        dtype = f[13]
        if cmd == 0x02 and dtype == 0x02 and len(f) >= 49:
            p = f[16:]
            if p[1] == 0x5A and p[2] == 0xA1:
                d = self.dev
                d.mode = p[3] & 0x0F
                d.gear = p[4] if 1 <= p[4] <= 6 else d.gear
                d.on = 1 if p[9] == 0 else 0
                d.pm25 = int.from_bytes(p[12:14], "big")
                d.online = True
                d.last_state_ts = time.time()
                self.got_state.set()
                if on_state:
                    on_state(d)
        elif cmd == 0x83:
            if len(f) > 17:
                self.dev.online = f[17] == 1
                if on_state:
                    on_state(self.dev)


def load_config(path=CONFIG_PATH):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_config(cfg, path=CONFIG_PATH):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
