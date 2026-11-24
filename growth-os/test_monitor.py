import importlib.util
import pathlib
import unittest
import json
p = pathlib.Path(__file__).with_name("monitor.py")
spec = importlib.util.spec_from_file_location("monitor", p)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
class Checks(unittest.TestCase):
    def test_http(self):
        self.assertEqual(m.evaluate("homepage", 503, ""), ["HTTP 503"])
    def test_campaign(self):
        self.assertEqual(m.evaluate("travel", 200, "See product details"), [])
        self.assertTrue(m.evaluate("family", 200, "no button"))
    def test_health(self):
        self.assertEqual(m.evaluate("health", 200, json.dumps(dict.fromkeys(("loaded","lab_ready","scheduled","audit_completed"),True))), [])
        self.assertTrue(m.evaluate("health", 200, "{}"))
    def test_full_audit_required(self):
        d = dict.fromkeys(("loaded","local_commerce","catalog","campaign_pages_provisioned","seo_drafts_created","scheduler_registered","full_audit_completed"), True)
        self.assertEqual(m.evaluate("operations", 200, json.dumps(d)), [])
        d["full_audit_completed"] = False
        self.assertEqual(m.evaluate("operations", 200, json.dumps(d)), ["full_audit_completed not ready"])
if __name__ == "__main__":
    unittest.main()
