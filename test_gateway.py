import copy
import json
import tempfile
from pathlib import Path
import unittest
from datetime import datetime, timedelta, timezone
from gateway import State, compute_status, load_panel, unavailable_status


class GatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = load_panel()

    def test_normal(self):
        r = compute_status(self.cfg, "normal")
        self.assertEqual(r["totalW"], 507)
        self.assertEqual({z["id"]:z["watts"] for z in r["zones"]}, {"A":0,"B":67,"C":440})
        self.assertFalse(r["mappingVerified"])

    def test_overload_is_demo_only(self):
        r = compute_status(self.cfg, "overload")
        a = next(z for z in r["zones"] if z["id"]=="A")
        self.assertEqual(a["watts"],1900)
        self.assertEqual(a["status"],"over")
        self.assertTrue(any("錯峰" in n["detail"] for n in r["recommendations"]))
        self.assertEqual(r["source"],"SIMULATED_DATA_ONLY")

    def test_smart_shifts_not_saves_energy(self):
        r = compute_status(self.cfg,"smart")
        a = next(z for z in r["zones"] if z["id"]=="A")
        self.assertEqual(a["requestedW"],1900)
        self.assertEqual(a["watts"],700)
        self.assertIn("吹風機",a["deferredDevices"])
        self.assertEqual(r["totalW"],700)
        self.assertTrue(any("總度數" in n["detail"] for n in r["recommendations"]))

    def test_mixed_scenario(self):
        r=compute_status(self.cfg,"mixed")
        self.assertEqual(r["totalW"],1207)

    def test_external_stale(self):
        r=compute_status(self.cfg,telemetry={"zones":{"A":100,"B":250,"C":150}})
        self.assertEqual(r["source"],"EXTERNAL_UNVERIFIED")
        self.assertTrue(r["stale"])
        self.assertEqual(r["totalW"],500)

    def test_external_fresh(self):
        now=datetime.now(timezone.utc).isoformat()
        r=compute_status(self.cfg,telemetry={"timestamp":now,"zones":{"A":100,"B":250,"C":150}})
        self.assertFalse(r["stale"])

    def test_external_missing_branch_rejected(self):
        with self.assertRaises(ValueError):
            compute_status(self.cfg,telemetry={"zones":{"A":100,"B":250}})

    def test_external_negative_rejected(self):
        with self.assertRaises(ValueError):
            compute_status(self.cfg,telemetry={"zones":{"A":-3,"B":300,"C":800}})

    def test_unavailable_does_not_fake_normal(self):
        r=unavailable_status(self.cfg, "test")
        self.assertIsNone(r["totalW"])
        self.assertTrue(r["stale"])
        self.assertFalse(r["zones"])

    def test_220_volt_branch_math_and_no_synthetic_main_current(self):
        cfg=copy.deepcopy(self.cfg)
        cfg["panel"]["branches"][2]["voltageV"]=220
        r=compute_status(cfg,"normal")
        c=next(z for z in r["zones"] if z["id"]=="C")
        self.assertAlmostEqual(c["currentA"],2.0)
        self.assertIsNone(r["totalAEstimated"])
        self.assertIsNone(r["mainLimitW"])

    def test_energy_integrates_only_valid_elapsed_samples(self):
        state=State(self.cfg)
        state.sample(0)
        state.sample(5)
        r=state.sample(10)
        self.assertAlmostEqual(r["energySinceStartKWh"],507*10/3600000,places=6)
        r=state.sample(100)
        self.assertAlmostEqual(r["energySinceStartKWh"],507*10/3600000,places=6)

    def test_external_error_is_not_simulated_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            f=Path(d)/"missing.json"
            s=State(self.cfg, f)
            r=s.sample(0)
            self.assertEqual(r["source"],"EXTERNAL_UNAVAILABLE")
            self.assertIsNone(r["totalW"])
            self.assertEqual(r["energySinceStartKWh"],0)


if __name__=="__main__":unittest.main()
