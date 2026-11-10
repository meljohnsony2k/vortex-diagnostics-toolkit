"""
Automated tests: vortex.py runs as a real command line program against the
simulated gateway in sim/fake_gateway.py (DoIP over TCP on 127.0.0.1:13400).

Run from the repo folder:   python -m unittest discover -v tests
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sim"))
from fake_gateway import FakeGateway, default_vehicle  # noqa: E402

VORTEX = os.path.join(ROOT, "vortex.py")
VIN = "WBASIM00000000001"


def read(path):
    with open(path) as f:
        return f.read()


def write(path, text):
    with open(path, "w") as f:
        f.write(text)


class VortexTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gw = FakeGateway(announce=True).start()
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        cls.gw.stop()

    def setUp(self):
        self.gw.vehicle = default_vehicle()     # fresh fake car for every test
        self.gw.log.clear()
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_cli(self, *args, stdin=None, ip=True):
        cmd = [sys.executable, VORTEX, *args]
        if ip and args[0] in ("read", "check", "battery", "istep", "faults", "diff") and "--compare" not in args:
            cmd += ["--ip", "127.0.0.1"]
        r = subprocess.run(cmd, cwd=self.dir, input=stdin, capture_output=True, text=True, timeout=60)
        return r.returncode, r.stdout + r.stderr

    # ---------- reading the car ----------
    def test_read_scan_finds_fitted_modules(self):
        code, out = self.run_cli("read", "--scan")
        self.assertEqual(code, 0, out)
        self.assertIn(f"VIN: {VIN}", out)
        res = json.loads(read(os.path.join(self.dir, f"vortex_read_{VIN}.json")))
        self.assertEqual(res["vin"], VIN)
        self.assertEqual(set(res["modules"]), {"10", "40", "60", "12"})        # 0x18 / 0x63 not fitted
        self.assertEqual(res["modules"]["60"]["SW version"], "ID5_SIM_20.11")
        self.assertNotIn("SW version", res["modules"]["12"])                   # DME refuses that identifier
        self.assertNotIn("Exception", out)                                     # library noise stays hidden

    def test_unfitted_module_is_skipped_after_first_reject(self):
        self.run_cli("read", "--scan")
        self.assertEqual(sum(1 for t, _ in self.gw.log if t == 0x18), 1)

    def test_auto_discovery_without_ip(self):
        code, out = self.run_cli("read", ip=False)
        self.assertEqual(code, 0, out)
        self.assertIn("Car at 127.0.0.1", out)
        self.assertIn(f"VIN: {VIN}", out)

    def test_no_car_gives_clear_error(self):
        r = subprocess.run([sys.executable, VORTEX, "read", "--ip", "127.0.0.2"], cwd=self.dir,
                           capture_output=True, text=True, timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Could not connect to the car", r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stdout + r.stderr)

    # ---------- battery ----------
    def set_volts(self, centivolts):
        self.gw.vehicle[0x10]["dids"][0xDD51] = centivolts.to_bytes(2, "big")

    def test_battery_levels(self):
        for cv, expect in ((1262, "12.62 V  -> OK"), (1210, "LOW - charger on"), (1180, "TOO LOW - do not flash")):
            self.set_volts(cv)
            code, out = self.run_cli("battery")
            self.assertIn(expect, out, f"{cv}: {out}")

    def test_battery_identifier_missing(self):
        del self.gw.vehicle[0x10]["dids"][0xDD51]
        code, out = self.run_cli("battery")
        self.assertIn("Battery voltage DID not found", out)

    # ---------- faults ----------
    def test_faults_read_and_clear_only_after_yes(self):
        code, out = self.run_cli("faults")
        self.assertIn("2 fault(s)", out)
        self.assertIn("801A20", out)
        self.run_cli("faults", "--clear", stdin="n\n")
        self.assertEqual(len(self.gw.vehicle[0x10]["dtcs"]), 2, "faults cleared without a yes")
        code, out = self.run_cli("faults", "--clear", stdin="y\n")
        self.assertIn("Fault memory cleared", out)
        code, out = self.run_cli("faults")
        self.assertIn("No stored faults", out)

    # ---------- safety gate + backups ----------
    def test_check_blocks_coding_until_backup_filed(self):
        code, out = self.run_cli("check")
        self.assertIn("STOP: no backup filed", out)
        ncd = os.path.join(self.dir, "HU.ncd")
        write(ncd, "backup")
        self.run_cli("backup", "add", "--vin", VIN, "--files", ncd)
        code, out = self.run_cli("check")
        self.assertIn("Clear to code", out)

    def test_backups_never_overwrite_each_other(self):
        ncd = os.path.join(self.dir, "HU.ncd")
        for content in ("first", "second", "third"):
            write(ncd, content)
            self.run_cli("backup", "add", "--vin", VIN, "--files", ncd)
        idx = json.loads(read(os.path.join(self.dir, "vortex_backups", "index.json")))
        folders = [b["folder"] for b in idx["cars"][VIN]["backups"]]
        self.assertEqual(len(set(folders)), 3)
        saved = [read(os.path.join(self.dir, "vortex_backups", f, "HU.ncd")) for f in folders]
        self.assertEqual(saved, ["first", "second", "third"])

    def test_backup_find_is_case_insensitive(self):
        ncd = os.path.join(self.dir, "HU.ncd")
        write(ncd, "x")
        self.run_cli("backup", "add", "--vin", VIN.lower(), "--files", ncd)
        code, out = self.run_cli("backup", "find", "--vin", VIN.lower())
        self.assertIn("1 backup(s)", out)

    # ---------- before / after snapshots ----------
    def test_diff_shows_module_change(self):
        self.run_cli("diff", "--module", "0x60", "--label", "before")
        self.gw.vehicle[0x60]["dids"][0xF195] = b"ID5_SIM_21.03"
        self.run_cli("diff", "--module", "0x60", "--label", "after")
        code, out = self.run_cli("diff", "--compare", "vortex_diff_60_before.json", "vortex_diff_60_after.json")
        self.assertIn("'ID5_SIM_20.11' -> 'ID5_SIM_21.03'", out)

    # ---------- offline tools ----------
    def test_export_and_job_card(self):
        self.run_cli("read")
        code, out = self.run_cli("export", "--from-read", f"vortex_read_{VIN}.json")
        self.assertEqual(json.loads(read(os.path.join(self.dir, "vortex_prefill.json")))[0]["vin"], VIN)
        code, out = self.run_cli("invoice", "--cust", "Demo", "--car", "F30 2016", "--vin", VIN, "--total", "120")
        html = read(os.path.join(self.dir, f"vortex_invoice_{VIN}.html"))
        self.assertIn(VIN, html)
        self.assertIn("$120.0", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
