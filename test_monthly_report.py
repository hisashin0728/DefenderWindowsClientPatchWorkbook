import json
import os
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import monthly_queries as queries

ROOT = Path(__file__).parent
PARAMETERS = {
    "SiteSel": "'*'", "OSSel": "'*'", "OnbSel": "'Onboarded'", "TypeSel": "'*'",
    "MonthCount": "6", "MonthSel": "latest",
}


def render(query):
    for name, value in PARAMETERS.items():
        query = query.replace("{" + name + "}", value)
    return query


def iter_items(items):
    for item in items:
        yield item
        if item["type"] == 12:
            yield from iter_items(item["content"]["items"])


def datatable(schema, rows):
    values = ",".join(
        json.dumps(value, ensure_ascii=True) if isinstance(value, str) else str(value)
        for row in rows for value in row
    )
    return f"datatable({schema})[{values}]"


EXPECTED = {
    "a-exact": "Installed", "a-later": "Installed", "a-old": "NotInstalled",
    "a-missing": "Unknown", "a-unpublished": "Unknown", "a-multiple": "Unknown",
    "a-partial": "Unknown", "b-platform": "Unknown",
    "b-client": "Installed", "b-newbase": "Unknown",
    "b-build-mismatch": "Unknown",
}


def fixture(inventory_time="ago(1h)", catalog_time="ago(1h)", inventory=True, devices=True,
            extra_catalog_rows=(), device_info_extra=""):
    device_rows = [
        (name, name, "GroupA" if name.startswith("a-") else "GroupB",
         "Windows10" if name == "b-platform" else "Windows11",
         26300 if name == "b-newbase" else (
             22621 if name == "b-build-mismatch" else (
                 26100 if name in ("b-platform", "b-server-unpublished", "b-server", "b-client") else 26200
             )
         ), "Workstation", "Onboarded")
        for name in EXPECTED
    ]
    device_rows += [
        ("not-onboarded", "discovered", "GroupA", "Windows11", 26200, "Workstation", "CanBeOnboarded"),
        ("linux", "linux", "GroupA", "Ubuntu", 0, "Workstation", "Onboarded"),
        ("b-server-unpublished", "server", "GroupB", "WindowsServer2025", 26100,
         "Server", "Onboarded"),
        ("b-server", "server", "GroupB", "WindowsServer2025", 26100, "Server", "Onboarded"),
        ("options-special", "discovered", "O'Hare \"Lab\"", "Windows11", 26200,
         "Workstation", "CanBeOnboarded"),
    ]
    catalog_rows = [
        ("Windows11", "10.0.26200.9711", 9711, -1, "B"),
        ("Windows11", "10.0.26200.9500", 9500, -2, "B"),
        ("Windows11", "10.0.26200.9818", 9818, -1, "Preview"),
        ("Windows11", "10.0.26200.9000", 9000, -3, "Other"),
        ("Windows11", "10.0.26100.9711", 9711, -1, "B"),
        ("Windows11", "10.0.26100.9500", 9500, -2, "B"),
        ("WindowsServer2025", "10.0.26100.33650", 33650, -1, "B"),
        ("WindowsServer2025", "10.0.26100.33158", 33158, -2, "B"),
        ("Windows11", "10.0.26300.9818", 9818, -1, "Preview"),
    ]
    catalog_rows.extend(extra_catalog_rows)
    inventory_rows = [
        ("a-exact", "Windows11", "10.0.26200.9711", "current"),
        ("a-later", "Windows11", "10.0.26200.9818", "current"),
        ("a-old", "Windows11", "10.0.26200.9000", "current"),
        ("a-unpublished", "Windows11", "10.0.26200.9999", "current"),
        ("a-multiple", "Windows11", "10.0.26200.9711", "current"),
        ("a-multiple", "Windows11", "10.0.26200.9818", "current"),
        ("a-partial", "Windows11", "10.0.26200.9711", "incomplete"),
        ("a-missing", "Windows11", "10.0.26200.9711", "previous"),
        ("b-platform", "Windows11", "10.0.26100.9711", "current"),
        ("b-server-unpublished", "WindowsServer2025", "10.0.26100.9711", "current"),
        ("b-server", "WindowsServer2025", "10.0.26100.33650", "current"),
        ("b-client", "Windows11", "10.0.26100.9711", "current"),
        ("b-newbase", "Windows11", "10.0.26300.9818", "current"),
        ("b-build-mismatch", "Windows11", "10.0.26200.9711", "current"),
    ]
    device_table = datatable(
        "DeviceId:string,DeviceName:string,MachineGroup:string,OSPlatform:string,OSBuild:long,"
        "DeviceType:string,OnboardingStatus:string", device_rows,
    )
    catalog_table = datatable(
        "Family:string,FullVersion:string,Revision:long,MonthOffset:int,ReleaseKind:string",
        catalog_rows,
    )
    inventory_table = datatable(
        "DeviceId:string,OSPlatform:string,SoftwareVersion:string,SnapshotId:string", inventory_rows,
    )
    history_table = datatable(
        "DeviceId:string,DeviceName:string,MachineGroup:string,OSPlatform:string,OSBuild:long,"
        "DeviceType:string,OnboardingStatus:string,AgeMinutes:int",
        [
            ("offboarded-now", "offboarded", "GroupA", "Windows11", 26200,
             "Workstation", "Onboarded", 120),
            ("offboarded-now", "offboarded", "GroupA", "Windows11", 26200,
             "Workstation", "CanBeOnboarded", 30),
            ("os-changed", "changed", "GroupA", "Windows11", 26200,
             "Workstation", "Onboarded", 120),
            ("os-changed", "changed", "GroupA", "Ubuntu", 0,
             "Workstation", "Onboarded", 30),
        ],
    )
    return f"""
let InventoryTestTime = {inventory_time};
let CatalogTestTime = {catalog_time};
let DeviceInfo = union
    ({device_table} | extend TimeGenerated = ago(1h)),
    ({history_table} | extend TimeGenerated = now() - AgeMinutes * 1m | project-away AgeMinutes)
| where {str(devices).lower()}
| extend OSVersion = '10.0',
         OsBuildRevision = case(DeviceId == 'a-old', '9000',
                                DeviceId == 'a-unpublished', '9999',
                                DeviceId == 'a-missing', '9500',
                                DeviceId in ('a-later', 'b-newbase'), '9818', '9711')
{device_info_extra};
let CatalogBuildRows = {catalog_table}
| extend TimeGenerated = CatalogTestTime, SnapshotId = 'catalog', RecordType = 'Build',
         BaseVersion = extract(@"^(\\d+\\.\\d+\\.\\d+)\\.\\d+$", 1, FullVersion),
         ReleaseMonth = datetime_add('month', MonthOffset, startofmonth(now())),
         PatchId = 'KB5999999', SourceUrl = 'https://support.microsoft.com/help/5999999'
| extend ReleaseDate = ReleaseMonth + 12d;
let WindowsMonthlyPatchCatalog_CL = union CatalogBuildRows,
    (print TimeGenerated = CatalogTestTime, SnapshotId = 'catalog', RecordType = 'Snapshot');
let InventoryBuildRows = {inventory_table}
| where {str(inventory).lower()}
| extend TimeGenerated = iff(SnapshotId == 'incomplete', now(), InventoryTestTime), RecordType = 'Device';
let DeviceWindowsOsInventory_CL = union InventoryBuildRows,
    (print TimeGenerated = InventoryTestTime, SnapshotId = 'current', RecordType = 'Snapshot'),
    (print TimeGenerated = InventoryTestTime - 1h, SnapshotId = 'previous', RecordType = 'Snapshot');
"""


class WorkbookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        subprocess.run(
            [sys.executable, str(ROOT / "build_workbook.py")],
            check=True, capture_output=True, encoding="utf-8",
        )
        cls.workbook = json.loads((ROOT / "YmcOSPatchStatus.workbook.json").read_text(encoding="utf-8"))
        cls.arm = json.loads((ROOT / "YmcOSPatchStatus.arm.json").read_text(encoding="utf-8"))
        cls.items = list(iter_items(cls.workbook["items"]))

    def test_primary_view_is_month_by_group_matrix(self):
        parameters = self.workbook["items"][1]["content"]["parameters"]
        by_name = {item["name"]: item for item in parameters}
        self.assertEqual(by_name["Tab"]["value"], "monthly")
        self.assertEqual(by_name["MonthCount"]["value"], "6")
        self.assertEqual(by_name["OnbSel"]["value"], ["Onboarded"])
        self.assertFalse({"SevSel", "OsRegex", "TrendRange"} & set(by_name))
        matrix = next(item for item in self.items if item["name"] == "monthly-matrix")
        self.assertEqual(matrix["content"]["visualization"], "table")
        self.assertIn("pivot(Site, take_any(Rate), MonthLabel)", matrix["content"]["query"])

    def test_all_query_parameters_are_defined(self):
        names = {item["name"] for item in self.workbook["items"][1]["content"]["parameters"]}
        for item in self.items:
            query = item.get("content", {}).get("query", "")
            self.assertLessEqual(set(re.findall(r"\{([A-Za-z][A-Za-z0-9]*)\}", query)), names)
        month = next(
            item for item in self.workbook["items"][1]["content"]["parameters"]
            if item["name"] == "MonthSel"
        )
        self.assertNotIn("{MonthSel}", month["query"])

    def test_queries_do_not_use_vulnerability_absence_as_installed(self):
        for item in self.items:
            query = item.get("content", {}).get("query", "")
            self.assertNotIn("DeviceTvmOsPatch_CL", query)
            self.assertNotIn("DeviceTvmSoftwareVulnerabilities", query)
        self.assertIn("isnull(KnownPublishedBuild)", queries.EVALUATION)
        self.assertIn("CurrentRevision >= RequiredRevision", queries.EVALUATION)
        self.assertIn("100.0 * Installed / Targets", queries.MONTHLY_SUMMARY)

    def test_windows_server_is_out_of_report_scope(self):
        self.assertNotIn("WindowsServer", queries.WINDOWS_CONDITION)
        self.assertIn("Family in ('windows10', 'windows11')", queries.CATALOG_SOURCE)

    def test_device_list_uses_deviceinfo_build_and_includes_updated_devices(self):
        item = next(item for item in self.items if item["name"] == "monthly-devices")
        self.assertEqual(item["content"]["query"], queries.DEVICE_DETAILS)
        self.assertNotIn("DeviceWindowsOsInventory_CL", queries.DEVICE_DETAILS)
        self.assertIn("tolong(OsBuildRevision)", queries.DEVICE_DETAILS)
        self.assertNotIn("Status !=", queries.DEVICE_DETAILS)
        self.assertIn("arg_max(ReachedMonth", queries.DEVICE_DETAILS)
        self.assertIn("lookup kind=leftouter KnownBuilds", queries.DEVICE_DETAILS)
        self.assertTrue({"FullVersion", "AppliedMonth", "AppliedPatchId", "TargetMonth"} <= {
            entry["columnId"] for entry in item["content"]["gridSettings"]["labelSettings"]
        })

    def test_initial_logs_request_budget_and_lazy_tabs(self):
        parameters = self.workbook["items"][1]["content"]["parameters"]
        log_parameters = [p for p in parameters if p.get("queryType") == 0 and p.get("query")]
        groups = [item for item in self.workbook["items"] if item["type"] == 12]
        self.assertEqual(len(groups), 7)
        for group in groups:
            self.assertEqual(group["content"]["loadType"], "lazy")
            self.assertEqual(group["conditionalVisibility"]["parameterName"], "Tab")
            self.assertTrue(all(
                "conditionalVisibility" not in child for child in group["content"]["items"]
            ))
        primary = next(
            group for group in groups if group["conditionalVisibility"]["value"] == "monthly"
        )
        self.assertEqual(len(log_parameters), 5)
        self.assertEqual(len([item for item in iter_items(primary["content"]["items"])
                              if item["type"] == 3]) + len(log_parameters), 8)

    def test_filter_options_are_direct_queries_defined_before_use(self):
        parameters = self.workbook["items"][1]["content"]["parameters"]
        by_name = {item["name"]: item for item in parameters}
        self.assertNotIn("DeviceOptions", by_name)
        for name in ("SiteSel", "OSSel", "OnbSel", "TypeSel"):
            target = by_name[name]
            self.assertEqual(target["type"], 2)
            self.assertTrue(target["multiSelect"])
            self.assertNotIn("jsonData", target)
            self.assertEqual(target["query"], queries.OPTION_QUERIES[name])
            self.assertEqual(target["queryType"], 0)
            self.assertEqual(target["crossComponentResources"], ["{Workspace}"])
            self.assertEqual(target["typeSettings"]["additionalResourceOptions"], ["value::all"])
            self.assertEqual(target["selectAllValue"], "*")
            self.assertLess(parameters.index(by_name["Workspace"]), parameters.index(target))
        self.assertEqual(by_name["OnbSel"]["value"], ["Onboarded"])

    def test_non_selected_month_views_do_not_depend_on_month_picker(self):
        for name in (
            "monthly-status", "monthly-matrix", "monthly-unknown-matrix",
            "monthly-line", "monthly-counts", "osver-dist", "osver-validation",
        ):
            item = next(item for item in self.items if item["name"] == name)
            self.assertNotIn("{MonthSel}", item["content"]["query"])
        self.assertIn("{MonthSel}", queries.TILES)
        self.assertIn("let CatalogMetadata = toscalar(", queries.CATALOG_SOURCE)
        self.assertIn("let InventoryMetadata = toscalar(", queries.INVENTORY_SOURCE)
        self.assertIn("lookup kind=leftouter KnownBuilds", queries.EVALUATION)
        self.assertIn("lookup kind=leftouter Baselines", queries.EVALUATION)

    def test_deployed_workbook_defaults_to_requested_workspace(self):
        content = json.loads(self.arm["variables"]["workbookContent"])
        workspace = content["items"][1]["content"]["parameters"][0]
        self.assertEqual(workspace["value"], "__WORKSPACE_RESOURCE_ID__")
        self.assertIn("parameters('workbookSourceId')", self.arm["resources"][0]["properties"]["serializedData"])
        self.assertIn("guid(toLower(", self.arm["parameters"]["workbookId"]["defaultValue"])


@unittest.skipUnless(os.environ.get("YMC_COMPILED_TEMPLATE"), "Compiled Bicep template not supplied")
class IngestionTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        text = Path(os.environ["YMC_COMPILED_TEMPLATE"]).read_text(encoding="utf-8-sig")
        artifact, _ = json.JSONDecoder().raw_decode(text.lstrip())
        if "template" in artifact:
            if not artifact["success"]:
                raise AssertionError(artifact.get("diagnostics"))
            artifact = json.loads(artifact["template"])
        resources = artifact["resources"]
        cls.variables = artifact.get("variables", {})
        cls.resources = list(resources.values()) if isinstance(resources, dict) else list(resources)
        variable_reference = re.compile(r"\[variables\('([^']+)'\)(?:\.([A-Za-z0-9_]+))?\]")

        def constant_value(variables, match):
            constant = variables[match[1]]
            alias = variable_reference.fullmatch(constant) if isinstance(constant, str) else None
            if alias:
                constant = constant_value(variables, alias)
            if match[2]:
                constant = constant[match[2]]
            return constant

        def expand_constants(value, variables):
            if isinstance(value, dict):
                return {key: expand_constants(item, variables) for key, item in value.items()}
            if isinstance(value, list):
                return [expand_constants(item, variables) for item in value]
            if isinstance(value, str):
                match = variable_reference.fullmatch(value)
                if match:
                    constant = constant_value(variables, match)
                    if isinstance(constant, (dict, list)):
                        return expand_constants(constant, variables)
            return value

        def collect(template):
            found = []
            template_resources = template["resources"]
            template_resources = (
                list(template_resources.values()) if isinstance(template_resources, dict)
                else template_resources
            )
            for item in template_resources:
                if item["type"] == "Microsoft.Logic/workflows":
                    found.append((item, template.get("variables", {})))
                nested = item.get("properties", {}).get("template")
                if item["type"] == "Microsoft.Resources/deployments" and nested:
                    cls.resources.extend(
                        list(nested["resources"].values()) if isinstance(nested["resources"], dict)
                        else nested["resources"]
                    )
                    found.extend(collect(nested))
            return found

        workflows = collect(artifact)
        cls.workflows = {
            "inventory": next(item for item in workflows if "Run_Advanced_Hunting" in json.dumps(
                expand_constants(item[0]["properties"]["definition"]["actions"], item[1]))),
            "catalog": next(item for item in workflows if "Catalog_Initialize" in json.dumps(
                expand_constants(item[0]["properties"]["definition"]["actions"], item[1]))),
        }
        cls.actions = expand_constants(
            cls.workflows["inventory"][0]["properties"]["definition"]["actions"],
            cls.workflows["inventory"][1],
        )
        cls.catalog_actions = expand_constants(
            cls.workflows["catalog"][0]["properties"]["definition"]["actions"],
            cls.workflows["catalog"][1],
        )

    def test_inventory_and_catalog_are_separate_logic_apps(self):
        inventory, catalog = (self.workflows[name][0] for name in ("inventory", "catalog"))
        self.assertIsNot(inventory, catalog)
        self.assertEqual(inventory["identity"], {"type": "SystemAssigned"})
        self.assertEqual(catalog["identity"], {"type": "SystemAssigned"})
        self.assertEqual(set(self.actions), {
            "Snapshot_Time", "Run_Advanced_Hunting", "Inventory_Rows", "For_each_chunk",
            "Complete_Inventory_Snapshot",
        })
        self.assertEqual(set(self.catalog_actions), {
            "Snapshot_Time", "Catalog_Initialize", "Catalog_Collection", "Catalog_Failed",
        })
        self.assertNotIn("parameters", self.workflows["inventory"][0]["properties"]["definition"])
        self.assertIn("CatalogIngestionUri", self.workflows["catalog"][0]["properties"]["definition"]["parameters"])
        self.assertNotIn("Run_Advanced_Hunting", json.dumps(self.catalog_actions))
        self.assertNotIn("graph.microsoft.com/beta", json.dumps(self.actions))
        triggers = [
            self.workflows[name][0]["properties"]["definition"]["triggers"]["Daily"]["recurrence"]["schedule"]
            for name in ("inventory", "catalog")
        ]
        self.assertNotEqual(triggers[0], triggers[1])

    def test_each_logic_app_has_only_its_own_dcr_publisher_role(self):
        roles = [
            item for item in self.resources
            if item["type"] == "Microsoft.Authorization/roleAssignments"
        ]
        self.assertEqual(len(roles), 2)
        for role in roles:
            self.assertIn("3913510d-42f4-4e42-8a64-420c390055eb", role["properties"]["roleDefinitionId"])
            self.assertEqual(role["properties"]["principalType"], "ServicePrincipal")
        principals = {role["properties"]["principalId"] for role in roles}
        self.assertEqual(len(principals), 2)

    def test_snapshot_and_catalog_actions_are_at_workflow_root(self):
        self.assertEqual(
            self.actions["Complete_Inventory_Snapshot"]["runAfter"],
            {"For_each_chunk": ["Succeeded"]},
        )
        self.assertEqual(
            self.catalog_actions["Catalog_Initialize"]["runAfter"],
            {"Snapshot_Time": ["Succeeded"]},
        )
        self.assertEqual(self.catalog_actions["Snapshot_Time"]["inputs"], "@utcNow()")

    def test_inventory_collection_is_windows_client_only(self):
        self.assertIn('matches regex "^windows_(10|11)$"', self.variables["inventoryQuery"])
        self.assertNotIn("server_", self.variables["inventoryQuery"])

    def test_inventory_uses_graph_security_application_authentication(self):
        request = self.actions["Run_Advanced_Hunting"]["inputs"]
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["uri"], "https://graph.microsoft.com/v1.0/security/runHuntingQuery")
        self.assertEqual(request["headers"]["Content-Type"], "application/json")
        self.assertEqual(request["body"], {"Query": "[variables('inventoryQuery')]"})
        self.assertEqual(request["authentication"], {
            "type": "ManagedServiceIdentity", "audience": "https://graph.microsoft.com/",
        })

    def test_inventory_reads_graph_results_and_preserves_column_names(self):
        inventory = self.actions["Inventory_Rows"]["inputs"]
        self.assertEqual(inventory["from"], "@body('Run_Advanced_Hunting')?['results']")
        expected = {
            name: f"@item()['{name}']" for name in (
                "RecordType", "DeviceId", "DeviceName", "OSPlatform", "SoftwareName",
                "SoftwareVersion", "OSVersion", "OSArchitecture", "ProductCodeCpe",
            )
        }
        expected.update({
            "SnapshotId": "@workflow().run.name", "SnapshotTime": "@outputs('Snapshot_Time')",
        })
        self.assertEqual(inventory["select"], expected)

    def test_documented_inventory_examples_match_collection_contract(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8-sig")
        examples = [
            json.loads(block) for block in re.findall(
                r"^```json\r?\n(.*?)^```[ \t]*$", text, re.MULTILINE | re.DOTALL,
            )
        ]
        request = next(item for item in examples if isinstance(item, dict) and "Query" in item)
        response = next(item for item in examples if isinstance(item, dict) and "results" in item)
        device = next(
            item[0] for item in examples
            if isinstance(item, list) and item[0]["RecordType"] == "Device"
        )
        marker = next(
            item[0] for item in examples
            if isinstance(item, list) and item[0]["RecordType"] == "Snapshot"
        )
        self.assertEqual(request["Query"].strip(), self.variables["inventoryQuery"].strip())
        self.assertEqual(
            {item["name"]: item["type"] for item in response["schema"]},
            {name: "String" for name in response["results"][0]},
        )
        self.assertEqual(device, {
            **response["results"][0], "SnapshotId": device["SnapshotId"],
            "SnapshotTime": device["SnapshotTime"],
        })
        self.assertEqual(set(device), set(self.actions["Inventory_Rows"]["inputs"]["select"]))
        self.assertEqual(marker, {
            **{name: "" for name in device}, "SnapshotId": device["SnapshotId"],
            "SnapshotTime": device["SnapshotTime"], "RecordType": "Snapshot",
        })

    def test_http_and_control_action_shapes(self):
        extra = {
            "Http": set(), "Select": set(), "Compose": set(), "Wait": set(), "Terminate": set(),
            "ParseJson": set(), "Query": set(), "InitializeVariable": set(),
            "SetVariable": set(), "AppendToArrayVariable": set(), "Scope": {"actions"},
            "Foreach": {"foreach", "actions"}, "Until": {"expression", "limit", "actions"},
            "If": {"expression", "actions", "else"},
        }
        common = {"type", "inputs", "runAfter", "runtimeConfiguration", "operationOptions", "metadata"}

        def validate(actions):
            for name, action in actions.items():
                with self.subTest(action=name):
                    self.assertIn(action["type"], extra)
                    self.assertLessEqual(set(action), common | extra[action["type"]])
                    self.assertLessEqual(set(action.get("runAfter", {})), set(actions))
                    self.assertNotIn("retryPolicy", action)
                    if action["type"] == "ParseJson":
                        self.assertEqual(set(action["inputs"]), {"content", "schema"})
                    if "actions" in action:
                        validate(action["actions"])
                    if "else" in action:
                        validate(action["else"]["actions"])

        validate(self.actions)
        validate(self.catalog_actions)

    def test_catalog_is_native_and_failure_is_explicit(self):
        self.assertFalse(any(item["type"].startswith("Microsoft.Automation/") for item in self.resources))
        self.assertNotIn("/automationAccounts/", json.dumps(self.actions))
        self.assertNotIn("/automationAccounts/", json.dumps(self.catalog_actions))
        terminal = self.catalog_actions["Catalog_Failed"]
        self.assertEqual(terminal["inputs"]["runStatus"], "Failed")
        self.assertEqual(terminal["runAfter"], {"Catalog_Collection": ["Failed", "TimedOut"]})
        catalog = self.catalog_actions["Catalog_Collection"]["actions"]
        self.assertEqual(
            catalog["Catalog_Complete_Snapshot"]["runAfter"], {"Catalog_For_Each_Batch": ["Succeeded"]},
        )
        self.assertEqual(
            catalog["Catalog_For_Each_Batch"]["runAfter"], {"Catalog_Validate_Coverage": ["Succeeded"]},
        )

        def check_placement(actions, in_loop=False, nested=False):
            for action in actions.values():
                if action["type"] == "InitializeVariable":
                    self.assertFalse(nested)
                if action["type"] == "Terminate":
                    self.assertFalse(in_loop)
                loop = in_loop or action["type"] in ("Foreach", "Until")
                if "actions" in action:
                    check_placement(action["actions"], loop, True)
                if "else" in action:
                    check_placement(action["else"]["actions"], loop, True)

        check_placement(self.catalog_actions)
        check_placement(self.actions)


class GraphHuntingPermissionTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("pwsh"), "PowerShell 7 not available")
    def test_permission_helper_grants_only_graph_hunting_and_handles_failures(self):
        # Replace Graph commands with local stubs; never authenticate or change tenant permissions.
        command = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$source = [IO.File]::ReadAllText((Join-Path (Get-Location) 'ingest\grant-defender-api-permission.ps1'))
$source = $source -replace '(?m)^#requires[^\r\n]*(?:\r?\n|$)', ''
$helper = [scriptblock]::Create($source)
$script:IdentityId = '11111111-1111-1111-1111-111111111111'
$script:GraphId = '22222222-2222-2222-2222-222222222222'
$script:RoleId = '33333333-3333-3333-3333-333333333333'

function Connect-MgGraph {
    param([string[]] $Scopes, [switch] $NoWelcome)
    if ($Scopes.Count -ne 2 -or 'AppRoleAssignment.ReadWrite.All' -notin $Scopes -or
        'Application.Read.All' -notin $Scopes) { throw 'Unexpected administrator scopes.' }
}
function Get-MgServicePrincipal {
    param([string] $ServicePrincipalId, [string] $Filter, [string[]] $Property)
    if ($ServicePrincipalId) {
        if ($ServicePrincipalId -ne $script:IdentityId) { throw 'Unexpected target identity.' }
        return [pscustomobject]@{
            Id = $script:IdentityId
            ServicePrincipalType = $(if ($script:Scenario -eq 'wrong-target') { 'Application' } else { 'ManagedIdentity' })
        }
    }
    if ($Filter -ne "appId eq '00000003-0000-0000-c000-000000000000'") {
        throw 'Permissions must come from the Microsoft Graph service principal.'
    }
    $role = [pscustomobject]@{
        Id = $script:RoleId
        Value = 'ThreatHunting.Read.All'
        IsEnabled = $script:Scenario -ne 'disabled-role'
        AllowedMemberTypes = @($(if ($script:Scenario -eq 'delegated-role') { 'User' } else { 'Application' }))
    }
    $graph = [pscustomobject]@{ Id = $script:GraphId; AppRoles = @($role) }
    if ($script:Scenario -eq 'ambiguous-graph') { return @($graph, $graph) }
    return $graph
}
function Get-MgServicePrincipalAppRoleAssignment {
    param([string] $ServicePrincipalId, [switch] $All)
    if ($ServicePrincipalId -ne $script:IdentityId -or -not $All) {
        throw 'All assignments for the target identity must be checked.'
    }
    if ($script:Scenario -eq 'read-failure') { throw 'Assignment lookup failed.' }
    if ($script:Scenario -eq 'existing') {
        return [pscustomobject]@{ ResourceId = $script:GraphId; AppRoleId = $script:RoleId }
    }
    if ($script:Scenario -eq 'other-permission') {
        return [pscustomobject]@{
            ResourceId = $script:GraphId
            AppRoleId = '44444444-4444-4444-4444-444444444444'
        }
    }
}
function New-MgServicePrincipalAppRoleAssignment {
    param([string] $ServicePrincipalId, [string] $PrincipalId, [string] $ResourceId, [string] $AppRoleId)
    if ($ServicePrincipalId -ne $script:IdentityId -or $PrincipalId -ne $script:IdentityId -or
        $ResourceId -ne $script:GraphId -or $AppRoleId -ne $script:RoleId) {
        throw 'Unexpected application permission assignment.'
    }
    $script:Writes++
    if ($script:Scenario -eq 'write-failure') { throw 'Assignment creation failed.' }
}

$cases = @(
    @{ Name = 'new'; Fails = $false; Writes = 1 }
    @{ Name = 'existing'; Fails = $false; Writes = 0 }
    @{ Name = 'other-permission'; Fails = $false; Writes = 1 }
    @{ Name = 'wrong-target'; Fails = $true; Writes = 0 }
    @{ Name = 'disabled-role'; Fails = $true; Writes = 0 }
    @{ Name = 'delegated-role'; Fails = $true; Writes = 0 }
    @{ Name = 'ambiguous-graph'; Fails = $true; Writes = 0 }
    @{ Name = 'read-failure'; Fails = $true; Writes = 0 }
    @{ Name = 'write-failure'; Fails = $true; Writes = 1 }
)
foreach ($case in $cases) {
    $script:Scenario = $case.Name
    $script:Writes = 0
    $failure = $null
    try { & $helper -LogicAppPrincipalId $script:IdentityId }
    catch { $failure = $_ }
    if (($null -ne $failure) -ne $case.Fails -or $script:Writes -ne $case.Writes) {
        throw "Unexpected outcome for $($case.Name): $failure; writes=$script:Writes"
    }
}
"""
        result = subprocess.run(
            ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


@unittest.skipUnless(os.environ.get("YMC_LOG_ANALYTICS_TOKEN"), "Live query token not supplied")
class LiveCalculationTests(unittest.TestCase):
    def query(self, query):
        workspace = os.environ["YMC_TEST_WORKSPACE_ID"]
        request = Request(
            f"https://api.loganalytics.io/v1/workspaces/{workspace}/query",
            data=json.dumps({"query": query}).encode("utf-8"),
            headers={
                "Authorization": "Bearer " + os.environ["YMC_LOG_ANALYTICS_TOKEN"],
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=120) as response:
                body = json.load(response)
        except HTTPError as error:
            self.fail(error.read().decode("utf-8"))
        self.assertNotIn("error", body)
        table = body["tables"][0]
        columns = [column["name"] for column in table["columns"]]
        return [dict(zip(columns, row, strict=True)) for row in table["rows"]]

    def test_all_generated_grid_and_chart_bindings(self):
        subprocess.run(
            [sys.executable, str(ROOT / "build_workbook.py")],
            check=True, capture_output=True, encoding="utf-8",
        )
        book = json.loads((ROOT / "YmcOSPatchStatus.workbook.json").read_text(encoding="utf-8"))
        items = {item["name"]: item for item in iter_items(book["items"]) if item["type"] == 3}
        jobs = [(name, item["content"]["query"]) for name, item in items.items()]
        jobs += [
            ("parameter-" + parameter["name"], parameter["query"])
            for parameter in book["items"][1]["content"]["parameters"]
            if parameter.get("queryType") == 0 and parameter.get("query")
        ]
        for offset in range(0, len(jobs), 5):
            batch = jobs[offset:offset + 5]
            payload = {"requests": [{
                "id": name, "method": "POST", "path": "/query",
                "workspace": os.environ["YMC_TEST_WORKSPACE_ID"],
                "headers": {"Content-Type": "application/json"},
                "body": {"query": fixture() + render(query), "timespan": "P30D"},
            } for name, query in batch]}
            request = Request(
                "https://api.loganalytics.io/v1/$batch",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Authorization": "Bearer " + os.environ["YMC_LOG_ANALYTICS_TOKEN"],
                    "Content-Type": "application/json",
                },
            )
            with urlopen(request, timeout=180) as response:
                responses = json.load(response)["responses"]
            self.assertEqual(len(responses), len(batch))
            for result in responses:
                with self.subTest(query=result["id"]):
                    self.assertEqual(result["status"], 200, result.get("body"))
                    self.assertNotIn("error", result["body"])
                    columns = {
                        column["name"] for column in result["body"]["tables"][0]["columns"]
                    }
                    if result["id"] not in items:
                        continue
                    content = items[result["id"]]["content"]
                    grid = content.get("gridSettings", {})
                    self.assertLessEqual(
                        {label["columnId"] for label in grid.get("labelSettings", [])}, columns,
                    )
                    self.assertLessEqual(
                        {formatter["columnMatch"] for formatter in grid.get("formatters", [])}, columns,
                    )
                    chart = content.get("chartSettings", {})
                    bindings = set(chart.get("yAxis", [])) | {
                        chart[key] for key in ("xAxis", "group") if key in chart
                    }
                    self.assertLessEqual(bindings, columns)

    def test_exact_later_missing_unpublished_and_wrong_product_cases(self):
        rows = self.query(
            fixture() + render(queries.EVALUATION + queries.SELECTED_MONTH) +
            "Evaluations | where ReleaseMonth == SelectedMonth | project DeviceId, Status, Reason"
        )
        self.assertEqual({row["DeviceId"]: row["Status"] for row in rows}, EXPECTED)

    def test_device_update_list_includes_all_devices_without_tvm_inventory(self):
        prefix = fixture(inventory=False, inventory_time="ago(3d)")
        rows = self.query(prefix + render(queries.DEVICE_DETAILS))
        actual = {row["DeviceName"]: row for row in rows}
        self.assertEqual(set(actual), set(EXPECTED))
        self.assertEqual(len(rows), len(EXPECTED))
        latest = actual["a-exact"]["TargetMonth"]
        for name in ("a-exact", "a-later", "a-multiple", "a-partial", "b-client"):
            self.assertEqual(actual[name]["AppliedMonth"], latest)
            self.assertEqual(actual[name]["Status"], "対象月まで更新済み")
            self.assertEqual(actual[name]["AppliedFullVersion"].split(".")[-1], "9711")
        self.assertEqual(actual["a-missing"]["FullVersion"], "10.0.26200.9500")
        self.assertLess(actual["a-missing"]["AppliedMonth"], latest)
        self.assertEqual(actual["a-missing"]["Status"], "対象月未到達")
        for name in ("a-unpublished", "b-platform", "b-build-mismatch"):
            self.assertEqual(actual[name]["AppliedMonth"], "未評価")
            self.assertEqual(actual[name]["AppliedPatchId"], "")
        self.assertEqual(actual["b-newbase"]["AppliedMonth"], "該当する月例更新なし")
        self.assertEqual(actual["b-newbase"]["AppliedPatchId"], "")
        self.assertEqual(actual["b-newbase"]["Status"], "未評価")
        earlier = actual["a-missing"]["AppliedMonth"].replace("/", "-") + "-01"
        selected = queries.DEVICE_DETAILS.replace("{MonthSel}", earlier)
        past = {row["DeviceName"]: row for row in self.query(prefix + render(selected))}
        self.assertEqual(past["a-exact"]["AppliedMonth"], latest)
        self.assertEqual(past["a-exact"]["Status"], "対象月まで更新済み")

    def test_device_update_list_uses_full_catalog_history_and_oob_baselines(self):
        prefix = fixture(
            extra_catalog_rows=[
                ("Windows11", "10.0.26200.8200", 8200, -8, "B"),
                ("Windows11", "10.0.26200.9800", 9800, -1, "OOB"),
            ],
            device_info_extra="| extend OsBuildRevision = iff(DeviceId == 'a-later', '9800', OsBuildRevision)",
        )
        query = render(queries.DEVICE_DETAILS.replace("{MonthCount}", "2"))
        rows = self.query(prefix + query + """
| extend ExpectedOlderMonth = format_datetime(datetime_add('month', -8, startofmonth(now())), 'yyyy/MM')
""")
        actual = {row["DeviceName"]: row for row in rows}
        self.assertEqual(actual["a-old"]["AppliedMonth"], actual["a-old"]["ExpectedOlderMonth"])
        self.assertEqual(actual["a-old"]["AppliedFullVersion"], "10.0.26200.8200")
        self.assertEqual(actual["a-later"]["FullVersion"], "10.0.26200.9800")
        self.assertEqual(actual["a-later"]["AppliedFullVersion"], "10.0.26200.9711")
        self.assertEqual(actual["a-later"]["AppliedMonth"], actual["a-later"]["TargetMonth"])

    def test_device_update_list_keeps_invalid_and_missing_builds_as_unknown(self):
        prefix = fixture(device_info_extra="""
| extend OSVersion = iff(DeviceId == 'a-later', '22H2', OSVersion),
         OsBuildRevision = case(DeviceId == 'a-exact', 'invalid',
                                DeviceId == 'a-missing', '',
                                DeviceId == 'a-old', '-1',
                                DeviceId == 'a-multiple', '9223372036854775808', OsBuildRevision),
         TimeGenerated = iff(DeviceId == 'a-partial', now() + 1d, TimeGenerated)
""")
        rows = self.query(prefix + render(queries.DEVICE_DETAILS))
        actual = {row["DeviceName"]: row for row in rows}
        self.assertEqual(set(actual), set(EXPECTED))
        for name in ("a-exact", "a-later", "a-missing", "a-old", "a-multiple", "a-partial"):
            self.assertEqual(actual[name]["AppliedMonth"], "未評価")
            self.assertEqual(actual[name]["AppliedPatchId"], "")
            self.assertEqual(actual[name]["AppliedFullVersion"], "")
            self.assertEqual(actual[name]["Status"], "未評価")
            self.assertTrue(actual[name]["Reason"])
        self.assertEqual(self.query(fixture(devices=False) + render(queries.DEVICE_DETAILS)), [])

    def test_device_update_list_rejects_stale_catalog_and_absent_revision_column(self):
        for prefix in (
            fixture(catalog_time="ago(3d)"),
            fixture(catalog_time="now() + 1d"),
            fixture(catalog_time="ago(30d)"),
            fixture(device_info_extra="| project-away OsBuildRevision"),
        ):
            with self.subTest(prefix=prefix[:100]):
                rows = self.query(prefix + render(queries.DEVICE_DETAILS))
                self.assertEqual(len(rows), len(EXPECTED))
                self.assertTrue(all(
                    row["AppliedMonth"] == "未評価" and row["Status"] == "未評価"
                    and row["AppliedPatchId"] == "" and row["Reason"] for row in rows
                ))

    def test_unknowns_stay_in_the_denominator(self):
        rows = self.query(
            fixture() + render(queries.MONTHLY_SUMMARY + queries.SELECTED_MONTH) +
            "MonthlySummary | where ReleaseMonth == SelectedMonth | project Site, Targets, Installed, Unknown, PatchRate"
        )
        actual = {row["Site"]: row for row in rows}
        self.assertEqual(actual["GroupA"], {
            "Site": "GroupA", "Targets": 7, "Installed": 2, "Unknown": 4, "PatchRate": 28.6,
        })
        self.assertEqual(actual["GroupB"], {
            "Site": "GroupB", "Targets": 4, "Installed": 1, "Unknown": 3, "PatchRate": 25,
        })

    def test_filter_options_match_the_previous_dropdown_values(self):
        old_query = (
            "let Original = DeviceInfo | where TimeGenerated > ago(14d) | where "
            + queries.WINDOWS_CONDITION + r""";
union
(Original | extend Site = iff(isempty(MachineGroup), '(未割当)', MachineGroup)
 | summarize by Site | project Key='SiteSel', Value=Site),
(Original | where isnotempty(OSPlatform) | summarize by OSPlatform
 | project Key='OSSel', Value=OSPlatform),
(Original | where isnotempty(OnboardingStatus) | summarize by OnboardingStatus
 | project Key='OnbSel', Value=OnboardingStatus),
(Original | where isnotempty(DeviceType) | summarize by DeviceType
 | project Key='TypeSel', Value=DeviceType)
"""
        )
        previous = self.query(fixture() + old_query)
        for key, query in queries.OPTION_QUERIES.items():
            rows = self.query(fixture() + query)
            self.assertTrue(all(row["value"] == row["label"] for row in rows))
            values = [row["value"] for row in rows]
            self.assertEqual(values, sorted(
                row["Value"] for row in previous if row["Key"] == key
            ))
            self.assertGreater(len(values), 0)
            if key == "SiteSel":
                self.assertIn("O'Hare \"Lab\"", values)

    def test_stale_empty_and_future_inventory_never_report_installed(self):
        variants = (
            fixture(inventory_time="ago(3d)"),
            fixture(inventory=False),
            fixture(inventory_time="now() + 1d"),
            fixture(catalog_time="ago(3d)"),
        )
        for variant in variants:
            with self.subTest(variant=variant[:100]):
                rows = self.query(
                    variant + render(queries.MONTHLY_SUMMARY) +
                    "MonthlySummary | project Targets, Installed, Unknown, PatchRate"
                )
                self.assertTrue(rows)
                self.assertTrue(all(
                    row["Installed"] == 0 and row["Targets"] == row["Unknown"]
                    and row["PatchRate"] is None for row in rows
                ))

    def test_matrix_has_month_rows_and_group_columns(self):
        rows = self.query(fixture() + render(queries.MATRIX))
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(set(row) == {"MonthLabel", "GroupA", "GroupB"} for row in rows))
        self.assertTrue(all(row["GroupA"] == "28.6%" and row["GroupB"] == "25.0%" for row in rows))

    def test_month_selector_returns_valid_first_of_month_values(self):
        rows = self.query(fixture() + render(queries.MONTH_OPTIONS))
        self.assertEqual(len(rows), 3)
        self.assertIn("latest", {row["value"] for row in rows})
        self.assertTrue(all(
            row["value"] == "latest" or re.fullmatch(r"\d{4}-\d{2}-01", row["value"])
            for row in rows
        ))

    def test_no_targets_is_not_100_percent(self):
        rows = self.query(fixture(devices=False) + render(queries.TILES))
        rate = next(row["Value"] for row in rows if row["Label"] == "確認済み適用率")
        self.assertEqual(rate, "対象なし")


if __name__ == "__main__":
    unittest.main()
