import copy
import json
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, Mock, call, patch
from urllib.error import HTTPError
from urllib.request import Request

from ingest import collect_monthly_catalog as catalog

NOW = datetime(2026, 11, 5, tzinfo=timezone.utc)


def revision(family="Windows11", month=10, classification="security", cadence="monthly"):
    build = 26200 if family == "Windows11" else 19045
    released = f"2026-{month:02d}-13T00:00:00Z"
    return {
        "id": f"10.0.{build}.9711", "product": "Windows 11" if family == "Windows11" else "Windows 10",
        "releaseDateTime": released, "isHotpatchUpdate": False,
        "osBuild": {"majorVersion": 10, "minorVersion": 0, "buildNumber": build, "updateBuildRevision": 9711},
        "catalogEntry": {
            "@odata.type": catalog.QUALITY_TYPE, "releaseDateTime": released,
            "qualityUpdateClassification": classification, "qualityUpdateCadence": cadence,
        },
        "knowledgeBaseArticle": {"id": "KB5127648", "url": "https://support.microsoft.com/help/5127648"},
    }


def product(family, count=6):
    return {
        "name": "Windows 11, version 25H2" if family == "Windows11" else "Windows 10, version 22H2",
        "groupName": "Windows 11" if family == "Windows11" else "Windows 10",
        "revisions": [revision(family, month=month) for month in range(11 - count, 11)],
    }


class CatalogTests(unittest.TestCase):
    def test_graph_classification_not_calendar_or_display_name(self):
        baseline = catalog.normalize_revision("Windows11", revision(), NOW)
        preview = catalog.normalize_revision("Windows11", revision(classification="nonSecurity"), NOW)
        oob = catalog.normalize_revision("Windows11", revision(cadence="outOfBand"), NOW)
        self.assertEqual([baseline["ReleaseKind"], preview["ReleaseKind"], oob["ReleaseKind"]],
                         ["B", "Preview", "OOB"])
        self.assertEqual(baseline["FullVersion"], "10.0.26200.9711")
        self.assertTrue(baseline["ReleaseMonth"].startswith("2026-10-01"))

    def test_feature_rtm_hotpatch_and_future_revisions_are_excluded(self):
        feature = {"catalogEntry": {"@odata.type": catalog.FEATURE_TYPE}}
        self.assertIsNone(catalog.normalize_revision("Windows11", feature, NOW))
        self.assertIsNone(catalog.normalize_revision("Windows11", {
            "catalogEntry": None, "knowledgeBaseArticle": None,
        }, NOW))
        hotpatch = revision()
        hotpatch["isHotpatchUpdate"] = True
        self.assertIsNone(catalog.normalize_revision("Windows11", hotpatch, NOW))
        self.assertIsNone(catalog.normalize_revision("Windows11", revision(month=11), NOW))

    def test_invalid_or_unknown_metadata_is_not_a_monthly_baseline(self):
        variants = []
        unknown = revision()
        unknown["catalogEntry"]["qualityUpdateClassification"] = "unknownFutureValue"
        variants.append(unknown)
        bad_build = revision()
        bad_build["osBuild"]["updateBuildRevision"] = True
        variants.append(bad_build)
        missing_kb = revision()
        missing_kb["knowledgeBaseArticle"] = None
        variants.append(missing_kb)
        unknown_hotpatch = revision()
        unknown_hotpatch.pop("isHotpatchUpdate")
        variants.append(unknown_hotpatch)
        mismatch = revision()
        mismatch["catalogEntry"]["releaseDateTime"] = "2026-09-08T00:00:00Z"
        variants.append(mismatch)
        for value in variants:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    catalog.normalize_revision("Windows11", value, NOW)

    def test_wrong_product_and_kb_host_are_rejected(self):
        with self.assertRaises(ValueError):
            catalog.normalize_revision("Windows10", revision(), NOW)
        value = revision()
        value["knowledgeBaseArticle"]["url"] = "https://example.com/kb"
        with self.assertRaises(ValueError):
            catalog.normalize_revision("Windows11", value, NOW)

    def test_server_products_are_out_of_scope(self):
        self.assertIsNone(catalog.product_family({"name": "Windows Server 2022", "groupName": "Server"}))
        self.assertEqual(catalog.product_family({
            "name": "Windows 10, version 22H2", "groupName": "Previous versions",
        }), "Windows10")

    def test_collection_and_nested_revision_paging(self):
        client = catalog.GraphClient("synthetic-token")
        next_page = catalog.GRAPH_ROOT + "products?$skiptoken=next"
        with patch.object(client, "get_json", side_effect=[
            {"value": [{"id": "one"}], "@odata.nextLink": next_page},
            {"value": [{"id": "two"}]},
        ]):
            self.assertEqual([row["id"] for row in client.collection(catalog.PRODUCTS_URL)], ["one", "two"])
        with patch.object(client, "get_json", return_value={"value": [{"id": "second"}]}):
            rows = list(client.revisions({
                "revisions": [{"id": "first"}],
                "revisions@odata.nextLink": catalog.GRAPH_ROOT + "products/1/revisions?$skiptoken=next",
            }))
            self.assertEqual([row["id"] for row in rows], ["first", "second"])

    def test_paging_cycles_invalid_shapes_and_untrusted_urls_are_rejected(self):
        client = catalog.GraphClient("synthetic-token")
        with patch.object(client, "get_json", return_value={
            "value": [], "@odata.nextLink": catalog.PRODUCTS_URL,
        }):
            with self.assertRaises(ValueError):
                list(client.collection(catalog.PRODUCTS_URL))
        with patch.object(client, "get_json", return_value={"value": {}}):
            with self.assertRaises(ValueError):
                list(client.collection(catalog.PRODUCTS_URL))
        with self.assertRaises(ValueError):
            list(client.revisions({"revisions": [], "revisions@odata.nextLink": False}))
        for url in (
            "https://example.com/next", "http://graph.microsoft.com/beta/admin/windows/updates/products",
            "https://graph.microsoft.com/v1.0/users",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                catalog.graph_url(url)

    def test_collect_deduplicates_and_requires_six_months_for_both_clients(self):
        products = [product("Windows10"), product("Windows11")]
        products[0]["revisions"].append(copy.deepcopy(products[0]["revisions"][0]))
        client = Mock(spec=catalog.GraphClient)
        client.collection.return_value = iter(products)
        client.revisions.side_effect = lambda value: iter(value["revisions"])
        rows = catalog.collect(NOW, client)
        self.assertEqual(len(rows), 12)
        self.assertEqual({row["Family"] for row in rows}, {"Windows10", "Windows11"})
        client.collection.return_value = iter([product("Windows10", count=5), product("Windows11")])
        with self.assertRaises(ValueError):
            catalog.collect(NOW, client)

    def test_failed_collection_never_uploads_or_writes_a_completion_marker(self):
        with patch("sys.argv", ["collector"]), patch.dict(os.environ, {}, clear=True), \
                patch.object(catalog, "identity_token", return_value="synthetic-token"), \
                patch.object(catalog, "collect", side_effect=ValueError("incomplete graph data")), \
                patch.object(catalog, "publish") as publish:
            with self.assertRaises(ValueError):
                catalog.main()
            publish.assert_not_called()

    def test_graph_and_monitor_tokens_are_separate_and_marker_is_last(self):
        record = catalog.normalize_revision("Windows11", revision(), NOW)
        endpoint = "https://example.centralus-1.ingest.monitor.azure.com"
        with patch("sys.argv", [
            "collector", "--endpoint", endpoint, "--dcr-id", "dcr-" + "a" * 32,
        ]), patch.dict(os.environ, {}, clear=True), \
                patch.object(catalog, "collect", return_value=[record]), \
                patch.object(catalog, "identity_token", side_effect=[
                    "graph-token", "monitor-data-token", "monitor-marker-token",
                ]) as identity, patch.object(catalog, "request_bytes") as request:
            catalog.main()
            self.assertEqual(identity.call_args_list, [call(catalog.GRAPH_RESOURCE), call(), call()])
            first, last = [entry.args[0] for entry in request.call_args_list]
            self.assertEqual(first.get_header("Authorization"), "Bearer monitor-data-token")
            self.assertEqual(last.get_header("Authorization"), "Bearer monitor-marker-token")
            self.assertEqual(json.loads(first.data)[0]["RecordType"], "Build")
            self.assertEqual(json.loads(last.data)[0]["RecordType"], "Snapshot")
            self.assertEqual(json.loads(first.data)[0]["SnapshotId"], json.loads(last.data)[0]["SnapshotId"])

    def test_dry_run_never_requests_an_ingestion_token(self):
        record = catalog.normalize_revision("Windows11", revision(), NOW)
        with patch("sys.argv", ["collector", "--dry-run"]), patch.dict(os.environ, {}, clear=True), \
                patch.object(catalog, "collect", return_value=[record]), \
                patch.object(catalog, "identity_token", return_value="graph-token") as identity, \
                patch.object(catalog, "publish") as publish:
            catalog.main()
            identity.assert_called_once_with(catalog.GRAPH_RESOURCE)
            publish.assert_not_called()

    def test_retry_after_is_respected_but_forbidden_requests_are_not_retried(self):
        response = MagicMock()
        response.read.return_value = b"{}"
        response.url = catalog.PRODUCTS_URL
        response.headers.get_content_charset.return_value = "utf-8"
        context = MagicMock()
        context.__enter__.return_value = response
        with patch.object(catalog, "build_opener") as opener, patch.object(catalog.time, "sleep") as sleep:
            opener.return_value.open.side_effect = [
                HTTPError(catalog.PRODUCTS_URL, 429, "throttled", {"Retry-After": "3"}, None), context,
            ]
            self.assertEqual(catalog.request_bytes(Request(catalog.PRODUCTS_URL))[0], b"{}")
            sleep.assert_called_once_with(3)
        with patch.object(catalog, "build_opener") as opener, patch.object(catalog.time, "sleep") as sleep:
            opener.return_value.open.side_effect = HTTPError(catalog.PRODUCTS_URL, 403, "forbidden", {}, None)
            with self.assertRaises(HTTPError):
                catalog.request_bytes(Request(catalog.PRODUCTS_URL))
            sleep.assert_not_called()

    def test_authenticated_redirect_is_rejected(self):
        with self.assertRaises(HTTPError):
            catalog.NoRedirect().redirect_request(
                Request(catalog.PRODUCTS_URL), None, 302, "redirect", {}, "https://example.com",
            )

    def test_upload_batches_preserve_all_records_and_byte_limit(self):
        rows = [{"value": "a" * 17, "i": index} for index in range(17)]
        batches = list(catalog.upload_batches(rows, maximum_bytes=120))
        self.assertTrue(all(len(batch) <= 120 for batch in batches))
        self.assertEqual([row for batch in batches for row in json.loads(batch)], rows)
        with self.assertRaises(ValueError):
            list(catalog.upload_batches([{"value": "a" * 100}], maximum_bytes=50))

    def test_missing_managed_identity_is_an_error(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                catalog.identity_token()


if __name__ == "__main__":
    unittest.main()
