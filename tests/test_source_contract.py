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
            self.assertEqual(records[0]["filename"], records[1]["filename"])
            first = client.fetch(records[0]["url"])
            second = client.fetch(records[1]["url"])
            self.assertEqual(len(first.body), len(second.body))
            self.assertNotEqual(first.body, second.body)
            self.assertNotEqual(first.headers["etag"], second.headers["etag"])
            self.assertEqual(client.fetch(records[2]["url"]).body, first.body)

    def test_every_record_has_the_declared_source_identity(self):
        scenario = load_scenario()
        for record in scenario["source_records"]:
            for field in scenario["identity_fields"]:
                self.assertIn(field, record)
                self.assertNotEqual(str(record[field]).strip(), "")


if __name__ == "__main__":
    unittest.main()
