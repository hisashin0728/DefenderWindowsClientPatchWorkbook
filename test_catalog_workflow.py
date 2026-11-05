import base64
import copy
import json
import re
import shutil
import subprocess
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from ingest import collect_monthly_catalog as reference
from test_monthly_catalog import NOW, revision

ROOT = Path(__file__).parent
WORKFLOW = json.loads((ROOT / "ingest" / "catalog-actions.json").read_text(encoding="utf-8"))


def all_actions(actions):
    for name, action in actions.items():
        yield name, action
        yield from all_actions(action.get("actions", {}))
        yield from all_actions(action.get("else", {}).get("actions", {}))


ACTIONS = dict(all_actions(WORKFLOW))


def runtime_schema(value):
    """Apply WDL's literal-at-sign escaping before validating a Parse JSON schema."""
    if isinstance(value, dict):
        if {"pattern", "patternProperties"} & set(value):
            raise ValueError("Logic Apps Parse JSON does not support regex schema keywords")
        return {runtime_schema(key): runtime_schema(item) for key, item in value.items()}
    if isinstance(value, list):
        return [runtime_schema(item) for item in value]
    if isinstance(value, str) and value.startswith("@"):
        if not value.startswith("@@"):
            raise ValueError(f"Unescaped WDL expression in static JSON schema: {value}")
        return value[1:]
    return value


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def unique(*arrays):
    result = []
    for array in arrays:
        for item in array:
            if item not in result:
                result.append(item)
    return result


class Expression:
    """Evaluate the pure WDL functions used by catalog mapping and guard fixtures."""

    def __init__(self, expression, *, row=None, bodies=None, outputs=None, variables=None, items=None):
        self.text = expression.removeprefix("@")
        self.position = 0
        self.functions = {
            "item": lambda: row,
            "items": lambda name: (items or {})[name],
            "body": lambda name: (bodies or {})[name],
            "outputs": lambda name: (outputs or {})[name],
            "variables": lambda name: (variables or {})[name],
            "workflow": lambda: {"run": {"name": "fixture-snapshot"}},
            "concat": lambda *values: "".join(values),
            "string": lambda value: "" if value is None else (
                value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            ),
            "if": lambda condition, yes, no: yes if condition else no,
            "equals": lambda left, right: left == right,
            "and": lambda *values: all(values),
            "or": lambda *values: any(values),
            "not": lambda value: not value,
            "empty": lambda value: value is None or value == "" or value == [] or value == {},
            "contains": lambda collection, value: value in collection,
            "createArray": lambda *values: list(values),
            "startsWith": lambda value, prefix: value.startswith(prefix),
            "endsWith": lambda value, suffix: value.endswith(suffix),
            "toUpper": lambda value: value.upper(),
            "toLower": lambda value: value.lower(),
            "substring": lambda value, start: value[start:],
            "replace": lambda value, old, new: value.replace(old, new),
            "split": lambda value, separator: value.split(separator),
            "uriScheme": lambda value: urlsplit(value).scheme,
            "uriHost": lambda value: urlsplit(value).hostname,
            "uriPort": lambda value: urlsplit(value).port or (
                443 if urlsplit(value).scheme.lower() == "https" else 80
            ),
            "coalesce": lambda *values: next(value for value in values if value is not None),
            "length": len,
            "union": unique,
            "base64": lambda value: base64.b64encode(value.encode("utf-8")).decode("ascii"),
            "mul": lambda left, right: left * right,
            "div": lambda left, right: left // right,
            "lessOrEquals": lambda left, right: left <= right,
            "greaterOrEquals": lambda left, right: left >= right,
            "ticks": lambda value: timestamp(value),
            "formatDateTime": lambda value, pattern: timestamp(value).strftime(
                {"yyyy-MM": "%Y-%m"}[pattern]
            ),
            "startOfMonth": lambda value: timestamp(value).replace(
                day=1, hour=0, minute=0, second=0, microsecond=0,
            ).isoformat(),
        }

    def evaluate(self):
        value = self.parse()
        self.space()
        if self.position != len(self.text):
            raise ValueError(f"Unparsed WDL at {self.text[self.position:]}")
        return value

    def space(self):
        while self.position < len(self.text) and self.text[self.position].isspace():
            self.position += 1

    def take(self, expected):
        self.space()
        if not self.text.startswith(expected, self.position):
            raise ValueError(f"Expected {expected} at {self.text[self.position:]}")
        self.position += len(expected)

    def quoted(self):
        self.take("'")
        result = ""
        while self.position < len(self.text):
            char = self.text[self.position]
            self.position += 1
            if char != "'":
                result += char
            elif self.text.startswith("'", self.position):
                result += "'"
                self.position += 1
            else:
                return result
        raise ValueError("Unterminated WDL string")

    def identifier(self):
        self.space()
        match = re.match(r"[A-Za-z_][A-Za-z0-9_]*", self.text[self.position:])
        if match is None:
            raise ValueError(f"Expected identifier at {self.text[self.position:]}")
        self.position += len(match[0])
        return match[0]

    def parse(self):
        self.space()
        if self.text[self.position] == "'":
            value = self.quoted()
        elif self.text[self.position].isdigit():
            match = re.match(r"\d+", self.text[self.position:])
            self.position += len(match[0])
            value = int(match[0])
        else:
            name = self.identifier()
            literals = {"true": True, "false": False, "null": None}
            if name in literals:
                value = literals[name]
            else:
                self.take("(")
                arguments = []
                self.space()
                if self.text[self.position] != ")":
                    while True:
                        arguments.append(self.parse())
                        self.space()
                        if self.text[self.position] != ",":
                            break
                        self.take(",")
                self.take(")")
                value = self.functions[name](*arguments)
        while True:
            self.space()
            if self.position == len(self.text):
                return value
            optional = self.text[self.position] == "?"
            if optional:
                self.position += 1
            if self.text[self.position] == "[":
                self.take("[")
                key = self.parse()
                self.take("]")
                value = value.get(key) if optional and isinstance(value, dict) else (
                    None if optional and value is None else value[key]
                )
            elif self.text[self.position] == ".":
                self.take(".")
                value = value[self.identifier()]
            else:
                return value


def evaluate(value, **context):
    if isinstance(value, str) and value.startswith("@"):
        return Expression(value, **context).evaluate()
    return value


def guard(name, **context):
    return evaluate(ACTIONS[name]["inputs"]["content"]["valid"], **context)


class NativeCatalogTests(unittest.TestCase):
    def test_parse_json_metadata_names_are_escaped_for_workflow_runtime(self):
        for action in ACTIONS.values():
            if action["type"] == "ParseJson":
                runtime_schema(action["inputs"]["schema"])
        schema = runtime_schema(ACTIONS["Catalog_Parse_Products"]["inputs"]["schema"])
        self.assertIn("@odata.nextLink", schema["properties"])
        schema = runtime_schema(ACTIONS["Catalog_Parse_Quality_Revisions"]["inputs"]["schema"])
        entry = schema["items"]["properties"]["catalogEntry"]
        self.assertIn("@odata.type", entry["required"])
        self.assertIn("@odata.type", entry["properties"])
        with self.assertRaises(ValueError):
            runtime_schema({"type": "string", "pattern": ".*"})

    def test_product_family_prefers_group_and_supports_previous_version_names(self):
        condition = ACTIONS["Catalog_Filter_Products"]["inputs"]["where"]
        family_expression = ACTIONS["Catalog_Product_Family"]["inputs"]
        for product in (
            {"groupName": "Windows 10", "name": "Windows 10, version 22H2"},
            {"groupName": "Windows 11", "name": "Windows 11, version 25H2"},
            {"groupName": "Previous versions", "name": "Windows 10, version 22H2"},
            {"groupName": "Windows 11", "name": "Windows 10, inconsistent display name"},
            {"groupName": "Server", "name": "Windows Server 2022"},
        ):
            expected = reference.product_family(product)
            self.assertEqual(evaluate(condition, row=product), expected is not None)
            if expected:
                self.assertEqual(evaluate(family_expression, items={
                    "Catalog_For_Each_Product": product,
                }), expected)

    def test_normalization_matches_reference_for_both_clients_and_all_kinds(self):
        mapping = ACTIONS["Catalog_Normalize_Builds"]["inputs"]["select"]
        for family in ("Windows10", "Windows11"):
            for classification, cadence in (
                ("security", "monthly"), ("nonSecurity", "monthly"),
                ("all", "monthly"), ("security", "outOfBand"), ("all", "outOfBand"),
            ):
                for kb_id in ("KB5127648", "5127648", "kb5127648"):
                    with self.subTest(family=family, classification=classification, cadence=cadence, kb=kb_id):
                        row = revision(family, classification=classification, cadence=cadence)
                        row["knowledgeBaseArticle"]["id"] = kb_id
                        expected = reference.normalize_revision(family, row, NOW)
                        actual = {
                            key: evaluate(value, row=row, outputs={
                                "Catalog_Product_Family": family, "Snapshot_Time": NOW.isoformat(),
                            }) for key, value in mapping.items()
                        }
                        self.assertEqual(actual.pop("SnapshotId"), "fixture-snapshot")
                        self.assertEqual(actual.pop("SnapshotTime"), NOW.isoformat())
                        for key in ("ReleaseMonth", "ReleaseDate"):
                            self.assertEqual(timestamp(actual.pop(key)), timestamp(expected.pop(key)))
                        self.assertEqual(actual, expected)

    def test_feature_rtm_hotpatch_future_and_mismatched_month_filters(self):
        quality = ACTIONS["Catalog_Quality_Candidates"]["inputs"]["where"]
        self.assertFalse(evaluate(quality, row={"catalogEntry": None, "knowledgeBaseArticle": None}))
        self.assertFalse(evaluate(quality, row={"catalogEntry": {"@odata.type": reference.FEATURE_TYPE}}))
        self.assertTrue(evaluate(quality, row=revision()))
        hotpatch = revision()
        hotpatch["isHotpatchUpdate"] = True
        self.assertFalse(evaluate(ACTIONS["Catalog_Exclude_Hotpatch"]["inputs"]["where"], row=hotpatch))
        published = ACTIONS["Catalog_Published_Revisions"]["inputs"]["where"]
        self.assertTrue(evaluate(published, row=revision(), outputs={"Snapshot_Time": NOW.isoformat()}))
        self.assertFalse(evaluate(published, row=revision(month=11), outputs={"Snapshot_Time": NOW.isoformat()}))
        mismatch = revision()
        mismatch["catalogEntry"]["releaseDateTime"] = "2026-09-08T00:00:00Z"
        invalid_month = ACTIONS["Catalog_Invalid_Months"]["inputs"]["where"]
        self.assertTrue(evaluate(invalid_month, row=mismatch))
        self.assertFalse(evaluate(invalid_month, row=revision()))

    def test_explicit_utc_validation_accepts_only_zero_offset_times(self):
        expression = ACTIONS["Catalog_Invalid_UTC_Dates"]["inputs"]["where"]
        for value, valid in (
            ("2026-10-13T00:00:00Z", True),
            ("2026-10-13T00:00:00+00:00", True),
            ("2026-10-13T00:00:00", False),
            ("2026-10-13T00:00:00+09:00", False),
        ):
            row = revision()
            row["releaseDateTime"] = value
            self.assertEqual(evaluate(expression, row=row), not valid)

    def test_explicit_kb_validation_replaces_unsupported_regex_schema(self):
        mapping = ACTIONS["Catalog_KB_References"]["inputs"]["select"]
        expression = ACTIONS["Catalog_Invalid_KB_References"]["inputs"]["where"]
        for kb, url, valid in (
            ("KB5127648", "https://support.microsoft.com/help/5127648", True),
            ("5127648", "https://support.microsoft.com/help/5127648", True),
            ("kb000001", "https://support.microsoft.com:443/help/000001", True),
            ("KB51276x8", "https://support.microsoft.com/help/5127648", False),
            ("KB12345", "https://support.microsoft.com/help/12345", False),
            ("KB123456789", "https://support.microsoft.com/help/123456789", False),
            ("KB5127648", "http://support.microsoft.com/help/5127648", False),
            ("KB5127648", "https://support.microsoft.com.evil.example/help/5127648", False),
            ("KB5127648", "https://user@support.microsoft.com/help/5127648", False),
        ):
            row = revision()
            row["knowledgeBaseArticle"] = {"id": kb, "url": url}
            reference = {key: evaluate(value, row=row) for key, value in mapping.items()}
            self.assertEqual(evaluate(expression, row=reference), not valid)

    def test_trusted_paging_cycles_and_limit_completion_guards(self):
        trusted = reference.GRAPH_ROOT + "products?$skiptoken=next"
        for prefix, field, visited in (
            ("Product", "CatalogProductsNext", "CatalogVisitedProducts"),
            ("Revision", "CatalogRevisionsNext", "CatalogVisitedRevisions"),
        ):
            for url, seen, expected in (
                (trusted, [], True), (trusted, [trusted], False),
                ("https://graph.microsoft.com.example.com/beta/admin/windows/updates/products", [], False),
                ("https://graph.microsoft.com/v1.0/users", [], False),
                ("http://graph.microsoft.com/beta/admin/windows/updates/products", [], False),
                (trusted + "#fragment", [], False),
            ):
                with self.subTest(prefix=prefix, url=url, seen=seen):
                    self.assertEqual(guard(f"Catalog_Validate_{prefix}_URL", variables={
                        field: url, visited: seen,
                    }), expected)
            self.assertFalse(guard(f"Catalog_Validate_{prefix}_Paging", variables={field: trusted, "CatalogHasError": False}))
            self.assertTrue(guard(f"Catalog_Validate_{prefix}_Paging", variables={field: "", "CatalogHasError": False}))
            self.assertFalse(guard(f"Catalog_Validate_{prefix}_Paging", variables={field: "", "CatalogHasError": True}))

    def test_paging_failure_stops_the_loop_and_cannot_complete_a_snapshot(self):
        for prefix, predecessor, next_variable in (
            ("Product", "Catalog_Product_Page", "CatalogProductsNext"),
            ("Revision", "Catalog_If_Revision_Page", "CatalogRevisionsNext"),
        ):
            failure = ACTIONS[f"Catalog_{prefix}_Page_Failed"]
            self.assertEqual(failure["runAfter"], {predecessor: ["Failed", "TimedOut"]})
            mark = failure["actions"][f"Catalog_Mark_{prefix}_Error"]
            stop = failure["actions"][f"Catalog_Stop_{prefix}_Pages"]
            self.assertEqual(mark["inputs"], {"name": "CatalogHasError", "value": True})
            self.assertEqual(stop["inputs"], {"name": next_variable, "value": ""})
            self.assertEqual(stop["runAfter"], {f"Catalog_Mark_{prefix}_Error": ["Succeeded"]})
        self.assertEqual(
            ACTIONS["Catalog_Build_Keys"]["runAfter"], {"Catalog_Validate_Product_Paging": ["Succeeded"]},
        )

    def test_catalog_http_uses_registered_graph_audience_and_monitor_for_uploads(self):
        for name in ("Catalog_Get_Products", "Catalog_Get_Revisions"):
            self.assertEqual(ACTIONS[name]["inputs"]["authentication"], {
                "type": "ManagedServiceIdentity", "audience": "https://graph.microsoft.com",
            })
        for name in ("Catalog_Send_Batch", "Catalog_Complete_Snapshot"):
            self.assertEqual(ACTIONS[name]["inputs"]["authentication"], {
                "type": "ManagedServiceIdentity", "audience": "https://monitor.azure.com",
            })

    def test_minimum_coverage_requires_six_distinct_months_for_each_client(self):
        months = [{"Month": f"2026-{month:02d}-01T00:00:00Z"} for month in range(5, 11)]
        for left, right, expected in (
            (months, months, True), (months[:5], months, False),
            (months, months[:5], False), ([months[0]] * 6, months, False),
            (months, [], False),
        ):
            with self.subTest(left=left, right=right):
                self.assertEqual(guard("Catalog_Validate_Coverage", bodies={
                    "Catalog_Months_Windows10": left, "Catalog_Months_Windows11": right,
                }), expected)

    def test_conflicting_duplicate_keys_fail_but_identical_rows_deduplicate(self):
        first = reference.normalize_revision("Windows11", revision(), NOW)
        second = dict(first, SourceUrl="https://support.microsoft.com/help/9999999")
        mapping = ACTIONS["Catalog_Build_Keys"]["inputs"]["select"]
        for rows, expected in ((unique([first], [first]), True), ([first, second], False)):
            keys = [{key: evaluate(value, row=row) for key, value in mapping.items()} for row in rows]
            self.assertEqual(guard("Catalog_Validate_Unique_Builds", variables={"CatalogRows": rows},
                                   bodies={"Catalog_Build_Keys": keys}), expected)

    def test_upload_guard_enforces_900000_utf8_bytes(self):
        for count in (899997, 899998, 899999, 900000, 900001, 900002, 900003):
            payload = '["' + "a" * (count - 4) + '"]'
            self.assertEqual(len(payload.encode("utf-8")), count)
            self.assertEqual(
                guard("Catalog_Validate_Batch_Size", outputs={"Catalog_Batch_JSON": payload}),
                count <= 900000,
            )
        for count in (299998, 299999, 300000):
            payload = json.dumps(["\u3042" * count], ensure_ascii=False, separators=(",", ":"))
            self.assertEqual(
                guard("Catalog_Validate_Batch_Size", outputs={"Catalog_Batch_JSON": payload}),
                len(payload.encode("utf-8")) <= 900000,
            )

    @unittest.skipUnless(shutil.which("pwsh"), "PowerShell 7 not available")
    def test_actual_parse_json_schemas_reject_invalid_graph_records(self):
        cases = []

        def case(action, value, valid):
            cases.append({"schema": runtime_schema(ACTIONS[action]["inputs"]["schema"]), "value": value, "valid": valid})

        base = revision()
        for action in ("Catalog_Parse_Quality_Revisions", "Catalog_Parse_Update_Metadata", "Catalog_Parse_Published_Builds"):
            case(action, [base], True)
        for field in ("knowledgeBaseArticle", "isHotpatchUpdate"):
            value = copy.deepcopy(base)
            value.pop(field)
            case("Catalog_Parse_Quality_Revisions", [value], False)
        for field, value in (("qualityUpdateCadence", "unknownFutureValue"), ("qualityUpdateClassification", "unknownFutureValue")):
            row = copy.deepcopy(base)
            row["catalogEntry"][field] = value
            case("Catalog_Parse_Update_Metadata", [row], False)
        for value in (True, "9711", -1):
            row = copy.deepcopy(base)
            row["osBuild"]["updateBuildRevision"] = value
            case("Catalog_Parse_Published_Builds", [row], False)
        row = copy.deepcopy(base)
        row["osBuild"]["buildNumber"] = 0
        case("Catalog_Parse_Published_Builds", [row], False)
        case("Catalog_Parse_Products", {"value": []}, True)
        case("Catalog_Parse_Products", {"value": {}, "@odata.nextLink": None}, False)
        case("Catalog_Parse_Products", {"value": [], "@odata.nextLink": False}, False)
        case("Catalog_Validate_Coverage", {"valid": True}, True)
        case("Catalog_Validate_Coverage", {"valid": False}, False)
        command = r"""
$ErrorActionPreference = 'Stop'
$cases = [Console]::In.ReadToEnd() | ConvertFrom-Json -Depth 100
foreach ($case in $cases) {
    $valid = $false
    try {
        $valid = Test-Json -Json (ConvertTo-Json -InputObject $case.value -Depth 100 -Compress) `
            -Schema (ConvertTo-Json -InputObject $case.schema -Depth 100 -Compress) -ErrorAction Stop
    } catch { $valid = $false }
    if ($valid -ne $case.valid) { throw ('Unexpected JSON schema outcome: ' + ($case | ConvertTo-Json -Depth 100 -Compress)) }
}
"""
        result = subprocess.run(
            ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
            input=json.dumps(cases), cwd=ROOT, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
