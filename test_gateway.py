import unittest
from gateway import compute_status, load_panel, recommendations


class GatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = load_panel()

    def test_normal(self):
        r = compute_status(self.cfg, "normal")
        self.assertEqual(r["totalW"], 507)
        self.assertEqual({z["id"]:z["watts"] for z in r["zones"]}, {"A":0,"B":67,"C":440})

    def test_overload_is_demo_only(self):
        r = compute_status(self.cfg, "overload")
        a = next(z for z in r["zones"] if z["id"]=="A")
        self.assertEqual(a["watts"], 1900)
        self.assertEqual(a["status"], "over")
        self.assertTrue(any("錯峰" in n["detail"] for n in r["recommendations"]))
        self.assertEqual(r["source"], "SIMULATED_DATA_ONLY")

    def test_shifted_use_is_not_physical_control(self):
        r = compute_status(self.cfg,"smart")
        a = next(z for z in r["zones"] if z["id"]=="A")
        self.assertEqual(a["requestedW"],1900)
        self.assertEqual(a["watts"],700)
        self.assertIn("吹風機",a["deferredDevices"])
        self.assertEqual(r["totalW"],700)

    def test_mixed(self):
        r=compute_status(self.cfg,"mixed")
        self.assertEqual(r["totalW"],1207)

    def test_external_is_unverified(self):
        r=compute_status(self.cfg,telemetry={"zones":{"A":100,"B":250,"C":150}})
        self.assertEqual(r["source"],"EXTERNAL_UNVERIFIED")
        self.assertTrue(r["stale"])
        self.assertEqual(r["totalW"],500)

    def test_external_rejects_invalid(self):
        with self.assertRaises(ValueError):
            compute_status(self.cfg,telemetry={"zones":{"A":-3,"B":300,"C":800}})


if __name__ == "__main__": unittest.main()
