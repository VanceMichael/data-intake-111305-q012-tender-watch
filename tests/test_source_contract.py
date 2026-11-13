import hashlib
import json
import unittest

from tender_watch.client import Client
from tender_watch.decoding import parse_page, records as decode_records
from tender_watch.origin import load_scenario, serve


class SourceContractTests(unittest.TestCase):
    def test_domain_observations_preserve_source_distinctions(self):
        scenario = load_scenario()
        records = scenario["source_records"]
        with serve(scenario) as origin:
            client = Client(origin.base_url)
            root = records[0]["descriptor"]
            response = client.fetch("/v2/vendor/tool/manifests/accepted")
            self.assertEqual("sha256:" + hashlib.sha256(response.body).hexdigest(), root["digest"])
            index = json.loads(response.body)
            manifest = index["manifests"][0]
            fetched = client.fetch("/v2/vendor/tool/manifests/" + manifest["digest"])
            self.assertEqual(len(fetched.body), manifest["size"])
            self.assertEqual("sha256:" + hashlib.sha256(fetched.body).hexdigest(), manifest["digest"])

    def test_every_record_has_the_declared_source_identity(self):
        scenario = load_scenario()
        for record in scenario["source_records"]:
            for field in scenario["identity_fields"]:
                self.assertIn(field, record)
                self.assertNotEqual(str(record[field]).strip(), "")


if __name__ == "__main__":
    unittest.main()
