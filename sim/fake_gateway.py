#!/usr/bin/env python3
"""
Simulated BMW gateway (ZGW) for testing vortex.py without a car.

Speaks ISO 13400 DoIP over TCP (routing activation + diagnostic messages)
and answers a small set of UDS services from several fake modules:

  0x22  ReadDataByIdentifier
  0x19  ReadDTCInformation (sub-function 0x02, report by status mask)
  0x14  ClearDiagnosticInformation

It also behaves like real modules sometimes do:
  - a module that is not fitted   -> DoIP negative acknowledge (unknown target)
  - an identifier a module lacks  -> UDS negative response 0x31 (request out of range)
  - one identifier that never answers -> client timeout

All data is made up. The VIN is a dummy, not a real vehicle.

Run on its own:   python sim/fake_gateway.py            (listens on 127.0.0.1:13400)
Then:             python vortex.py read --ip 127.0.0.1 --scan
"""
import argparse
import socket
import struct
import threading
import time

PROTO = 0x02
CLIENT_DEFAULT = 0x0E00

# payload types
ROUTING_REQ, ROUTING_RES = 0x0005, 0x0006
VEH_ID_REQ, VEH_ANNOUNCE = 0x0001, 0x0004
DIAG, DIAG_ACK, DIAG_NACK = 0x8001, 0x8002, 0x8003
ALIVE_REQ, ALIVE_RES = 0x0007, 0x0008


def pack(ptype, payload):
    return struct.pack("!BBHL", PROTO, 0xFF ^ PROTO, ptype, len(payload)) + payload


def default_vehicle():
    """Fake vehicle: logical address -> module data."""
    return {
        0x10: {  # gateway
            "name": "ZGW",
            "dids": {
                0xF190: b"WBASIM00000000001",          # dummy VIN (17 chars)
                0xF191: b"SIM-ZGW-0001",
                0xF195: b"ZGW_SIM_22.07",
                0xF197: b"ZGW",
                0xDD51: (1262).to_bytes(2, "big"),     # 12.62 V (x100)
            },
            "dtcs": [(0x801A20, 0x08), (0xE1A421, 0x2F)],
        },
        0x40: {"name": "BDC", "dids": {0xF191: b"SIM-BDC-0040", 0xF195: b"BDC_SIM_21.11", 0xF197: b"BDC"}, "dtcs": []},
        0x60: {"name": "HU_NBT_EVO", "dids": {0xF191: b"SIM-HU-0060", 0xF195: b"ID5_SIM_20.11", 0xF197: b"HU_NBT_EVO"},
               "dtcs": [(0xB7F2A1, 0x09)]},
        0x12: {"name": "DME", "dids": {0xF191: b"SIM-DME-0012", 0xF197: b"DME"},   # no 0xF195 -> NRC 0x31
               "dtcs": [], "silent_dids": {0xF1A0}},                            # 0xF1A0 never answers
        # 0x18 (cluster) and 0x63 are deliberately not fitted -> DoIP NACK
    }


class FakeGateway:
    def __init__(self, host="127.0.0.1", port=13400, vehicle=None, logical=0x10, announce=False):
        self.host, self.port, self.logical = host, port, logical
        self.vehicle = vehicle or default_vehicle()
        self.log = []               # (target, request hex) for tests
        self.announce = announce    # broadcast vehicle announcements (for auto discovery)
        self._srv = None
        self._stop = threading.Event()

    # ---------- UDS ----------
    def uds(self, target, req):
        ecu = self.vehicle[target]
        sid = req[0]
        if sid == 0x22 and len(req) >= 3:
            did = int.from_bytes(req[1:3], "big")
            if did in ecu.get("silent_dids", set()):
                return None                                   # no response -> timeout
            if did not in ecu["dids"]:
                return bytes([0x7F, 0x22, 0x31])
            return bytes([0x62]) + req[1:3] + ecu["dids"][did]
        if sid == 0x19 and len(req) >= 3 and req[1] == 0x02:
            mask = req[2]
            out = bytes([0x59, 0x02, 0xFF])
            for code, status in ecu["dtcs"]:
                if status & mask:
                    out += code.to_bytes(3, "big") + bytes([status])
            return out
        if sid == 0x14:
            ecu["dtcs"] = []
            return bytes([0x54])
        return bytes([0x7F, sid, 0x11])                       # service not supported

    # ---------- DoIP over TCP ----------
    def _client(self, conn):
        buf = b""
        conn.settimeout(0.2)
        while not self._stop.is_set():
            try:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
            except socket.timeout:
                continue
            except OSError:
                break
            while len(buf) >= 8:
                _, _, ptype, length = struct.unpack("!BBHL", buf[:8])
                if len(buf) < 8 + length:
                    break
                payload, buf = buf[8:8 + length], buf[8 + length:]
                self._handle(conn, ptype, payload)
        conn.close()

    def _handle(self, conn, ptype, payload):
        if ptype == ROUTING_REQ:
            client = struct.unpack("!H", payload[:2])[0]
            conn.sendall(pack(ROUTING_RES, struct.pack("!HHBL", client, self.logical, 0x10, 0)))
        elif ptype == ALIVE_REQ:
            conn.sendall(pack(ALIVE_RES, struct.pack("!H", self.logical)))
        elif ptype == DIAG:
            src, tgt = struct.unpack("!HH", payload[:4])
            req = payload[4:]
            self.log.append((tgt, req.hex()))
            if tgt not in self.vehicle:
                conn.sendall(pack(DIAG_NACK, struct.pack("!HHB", tgt, src, 0x03)))   # unknown target
                return
            conn.sendall(pack(DIAG_ACK, struct.pack("!HHB", tgt, src, 0x00)))
            res = self.uds(tgt, req)
            if res is not None:
                conn.sendall(pack(DIAG, struct.pack("!HH", tgt, src) + res))

    # ---------- lifecycle ----------
    def start(self):
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind((self.host, self.port))
        self._srv.listen(8)
        self._srv.settimeout(0.2)
        threading.Thread(target=self._accept, daemon=True).start()
        if self.announce:
            threading.Thread(target=self._announce_loop, daemon=True).start()
        return self

    def _announce_loop(self):
        """Send a DoIP vehicle announcement (UDP 13400) every 0.5 s, like a car after ignition on."""
        vin = self.vehicle[self.logical]["dids"][0xF190]
        msg = pack(VEH_ANNOUNCE, struct.pack("!17sH6s6sB", vin, self.logical, b"\x00SIM01", b"\x00SIM01", 0x00))
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        while not self._stop.is_set():
            try:
                s.sendto(msg, (self.host, 13400))
            except OSError:
                pass
            time.sleep(0.5)
        s.close()

    def _accept(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def stop(self):
        self._stop.set()
        try:
            self._srv.close()
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser(description="Simulated BMW gateway (DoIP/UDS) for testing vortex.py")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=13400)
    ap.add_argument("--announce", action="store_true", help="broadcast vehicle announcements so vortex.py can auto discover")
    a = ap.parse_args()
    gw = FakeGateway(a.host, a.port, announce=a.announce).start()
    print(f"[sim] Fake gateway on {a.host}:{a.port}  (Ctrl+C to stop)")
    print("[sim] Modules: " + ", ".join(f"0x{k:02X} {v['name']}" for k, v in gw.vehicle.items()))
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        gw.stop()


if __name__ == "__main__":
    main()
