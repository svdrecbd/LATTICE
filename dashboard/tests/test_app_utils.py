import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

try:
    import pandas  # noqa: F401
    import webview  # noqa: F401
except ImportError as exc:
    raise unittest.SkipTest(f"dashboard deps missing: {exc}")

import dashboard.app as app  # noqa: E402


class TestDashboardUtils(unittest.TestCase):
    def test_parse_endpoints_csv(self):
        text = "id,host,port,region,lat,lon\nnyc,1.2.3.4,9000,us-east,40.7,-74.0\n"
        endpoints = app.parse_endpoints_text(text)
        self.assertEqual(len(endpoints), 1)
        ep = endpoints[0]
        self.assertEqual(ep["id"], "nyc")
        self.assertEqual(ep["host"], "1.2.3.4")
        self.assertEqual(ep["port"], 9000)
        self.assertEqual(ep["regionHint"], "us-east")
        self.assertAlmostEqual(ep["lat"], 40.7)
        self.assertAlmostEqual(ep["lon"], -74.0)

    def test_parse_endpoints_json(self):
        text = '[{"id":"sto","host":"5.6.7.8","port":9000,"lat":59.3,"lon":18.0}]'
        endpoints = app.parse_endpoints_text(text)
        self.assertEqual(len(endpoints), 1)
        self.assertEqual(endpoints[0]["id"], "sto")
        self.assertEqual(endpoints[0]["host"], "5.6.7.8")

    def test_parse_probe_paths_csv(self):
        text = "id,bindInterface,bindIp\nvpn,,\ndirect,en0,\n"
        paths = app.parse_probe_paths_text(text)
        self.assertEqual(len(paths), 2)
        self.assertEqual(paths[1]["bindInterface"], "en0")

    def test_calibration_entry_base_id(self):
        cal = {"endpoints": {"a": {"biasMs": 5.0, "scale": 1.0}}}
        entry = app.calibration_entry(cal, "a@vpn")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["biasMs"], 5.0)

    def test_adjust_rtt(self):
        cal = {"endpoints": {"a": {"biasMs": 5.0, "scale": 2.0}}}
        adj = app.adjust_rtt_ms(9.0, "a", cal)
        self.assertAlmostEqual(adj, 2.0)
        adj2 = app.adjust_rtt_ms(3.0, "a", cal)
        self.assertAlmostEqual(adj2, 0.0)

    def test_build_calibration(self):
        cfg = {
            "endpoints": [
                {"id": "a", "host": "127.0.0.1", "port": 9000, "lat": 0.0, "lon": 0.0}
            ]
        }
        stats = {"a": {"p05": 12.0, "min": 12.0}}
        cal = app.build_calibration(cfg, stats, 0.0, 0.0, 200000.0, 1.0)
        self.assertIn("a", cal["endpoints"])
        self.assertAlmostEqual(cal["endpoints"]["a"]["biasMs"], 12.0)


if __name__ == "__main__":
    unittest.main()
