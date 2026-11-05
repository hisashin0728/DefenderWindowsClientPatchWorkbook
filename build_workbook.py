"""Sentinel Workbook for Microsoft monthly Windows patch compliance by device group.

Outputs:
  YmcOSPatchStatus.workbook.json  - workbook content (Advanced Editor > Gallery Template)
  YmcOSPatchStatus.arm.json       - ARM template that deploys the workbook to a Sentinel workspace
"""
import copy
import json
import os

from monthly_queries import (
    BASELINE_DETAILS, COUNTS, DEVICE_DETAILS, EVALUATION, MATRIX,
    MONTHLY_SUMMARY, MONTH_OPTIONS, OPTION_QUERIES, SOURCES, STATUS, TILES, UNKNOWN_MATRIX,
    WINDOWS_CONDITION,
)

HERE = os.path.dirname(os.path.abspath(__file__))
WS = "{Workspace}"


def kql(query, viz=None, name="", tab=None, title=None, size=0, grid=None, chart=None, tiles=None):
    content = {
        "version": "KqlItem/1.0",
        "query": query,
        "size": size,
        "queryType": 0,
        "resourceType": "microsoft.operationalinsights/workspaces",
        "crossComponentResources": [WS],
        "timeContext": {"durationMs": 2592000000},
    }
    if title:
        content["title"] = title
    if viz:
        content["visualization"] = viz
    if grid:
        content["gridSettings"] = grid
    if chart:
        content["chartSettings"] = chart
    if tiles:
        content["tileSettings"] = tiles
    item = {"type": 3, "content": content, "name": name}
    if tab:
        item["conditionalVisibility"] = {"parameterName": "Tab", "comparison": "isEqualTo", "value": tab}
    return item


def text(md, name, tab=None):
    item = {"type": 1, "content": {"json": md}, "name": name}
    if tab:
        item["conditionalVisibility"] = {"parameterName": "Tab", "comparison": "isEqualTo", "value": tab}
    return item


def labels(**kw):
    return [{"columnId": k, "label": v} for k, v in kw.items()]


RATE_FORMATTER = {
    "columnMatch": "PatchRate",
    "formatter": 18,
    "formatOptions": {
        "thresholdsOptions": "colors",
        "thresholdsGrid": [
            {"operator": "<", "thresholdValue": "80", "representation": "redBright", "text": "{0}{1}"},
            {"operator": "<", "thresholdValue": "95", "representation": "orange", "text": "{0}{1}"},
            {"operator": "Default", "representation": "green", "text": "{0}{1}"},
        ],
    },
}
BAR = lambda col: {"columnMatch": col, "formatter": 3, "formatOptions": {"palette": "blue"}}


def param(name, label, ptype, **kw):
    p = {"id": f"p-{name}", "version": "KqlParameterItem/1.0", "name": name, "label": label, "type": ptype}
    p.update(kw)
    return p


def options_param(name, label, selected=None):
    return param(
        name, label, 2,
        isRequired=False, multiSelect=True, quote="'", delimiter=",",
        query=OPTION_QUERIES[name], queryType=0,
        resourceType="microsoft.operationalinsights/workspaces",
        crossComponentResources=[WS],
        typeSettings={"additionalResourceOptions": ["value::all"], "showDefault": False},
        selectAllValue="*",
        value=["value::all"] if selected is None else selected,
    )

parameters = {
    "type": 9,
    "content": {
        "version": "KqlParameterItem/1.0",
        "parameters": [
            param(
                "Workspace", "Workspace (Defender データのあるワークスペースを選択)", 5, isRequired=True, multiSelect=False,
                query="where type =~ 'microsoft.operationalinsights/workspaces'\n| project value = id, label = name",
                typeSettings={
                    "resourceTypeFilter": {"microsoft.operationalinsights/workspaces": True},
                    "additionalResourceOptions": [], "showDefault": False,
                },
                queryType=1, resourceType="microsoft.resourcegraph/resources",
            ),
            param(
                "MonthCount", "表示月数", 2, isRequired=True, multiSelect=False, quote="",
                jsonData=json.dumps([{"value": str(n), "label": str(n)} for n in (3, 6, 12)]),
                value="6",
            ),
            options_param("SiteSel", "拠点 (デバイスグループ)"),
            options_param("OSSel", "OS プラットフォーム"),
            options_param("OnbSel", "オンボード状態", selected=["Onboarded"]),
            options_param("TypeSel", "デバイス種別"),
            param(
                "MonthSel", "台数・詳細の対象月", 2, isRequired=True, multiSelect=False, quote="",
                query=MONTH_OPTIONS, value="latest", queryType=0,
                resourceType="microsoft.operationalinsights/workspaces",
                crossComponentResources=[WS],
            ),
            param("Tab", "Tab", 1, isRequired=False, isHiddenWhenLocked=True, value="monthly"),
        ],
        "style": "pills",
        "queryType": 0,
        "resourceType": "microsoft.operationalinsights/workspaces",
    },
    "name": "parameters",
}


def tab_link(label, value):
    return {
        "id": f"tab-{value}", "cellValue": "Tab", "linkTarget": "parameter",
        "linkLabel": label, "subTarget": value, "style": "link",
    }


tabs = {
    "type": 11,
    "content": {
        "version": "LinkItem/1.0",
        "style": "tabs",
        "links": [
            tab_link("月次OSパッチ適用率", "monthly"),
            tab_link("台数内訳", "counts"),
            tab_link("月別グラフ", "chart"),
            tab_link("月例KB・対象ビルド", "detail"),
            tab_link("端末別更新状況", "devices"),
            tab_link("Windows OSビルド", "osver"),
            tab_link("データ診断", "diag"),
        ],
    },
    "name": "tabs",
}

items = []

items.append(text(
    "# Microsoft 月次OSパッチ適用状況 (Windows 10/11)\n"
    "行は月例セキュリティ更新の公開月、列は Defender デバイスグループです。"
    "**各月のパッチを最新の端末状態で評価**します。過去の月末時点の履歴ではありません。\n\n"
    "**適用率 = 当月の対象ビルド以上に到達した確認済み端末数 ÷ 対象Windows端末数 × 100**。"
    "対象はWindows 10/11のみです。同じOS・ビルド系列の後続累積更新も適用済みとします。"
    "Graph APIの公開ビルドと照合し、インベントリ欠落・不明なビルド・2日超のデータは未評価です。"
    "**未評価は分母に含めますが、適用済みには数えません。全台未評価のセルは「未評価」と表示します。**\n\n"
    "基準にするパッチは月例セキュリティ更新のみです。.NET・Office・Defender定義・プレビュー・緊急更新は基準行に含めません。"
    "これはOSの到達ビルドによる判定で、KBのインストール成功履歴ではありません。"
    "カタログ取得はMicrosoft Graph beta APIを使用します。",
    "header",
))
items.append(parameters)
items.append(tabs)

items.append(kql(
    STATUS, viz="table", name="monthly-status", tab="monthly", title="データ取込・評価状態",
    grid={"labelSettings": labels(
        InventoryLast="OS情報の最終取込 (UTC)", CatalogLast="対応表の最終取込 (UTC)",
        TargetDevices="対象Windows端末数", LatestPatchMonth="最新の月例更新", IngestionState="取込状態",
    )},
))
items.append(kql(
    TILES, viz="tiles", name="monthly-tiles", tab="monthly", size=4,
    title="選択月の台数・適用率 (台数・詳細の対象月)",
    tiles={
        "titleContent": {"columnMatch": "Label", "formatter": 1},
        "leftContent": {"columnMatch": "Value", "formatter": 1},
        "showBorder": True,
    },
))
items.append(kql(
    MATRIX, viz="table", name="monthly-matrix", tab="monthly",
    title="月次OSパッチ × デバイスグループ : 適用率", size=0,
    grid={"labelSettings": labels(MonthLabel="月次OSパッチ")},
))
items.append(kql(
    UNKNOWN_MATRIX, viz="table", name="monthly-unknown-matrix", tab="counts",
    title="未評価台数 (適用率の分母に含む)", size=0,
    grid={"labelSettings": labels(MonthLabel="月次OSパッチ")},
))
items.append(kql(
    MONTHLY_SUMMARY + """MonthlySummary
| project ReleaseMonth, Site, PatchRate
| sort by ReleaseMonth asc""",
    viz="linechart", name="monthly-line", tab="chart",
    title="月例更新ごとの適用率 (すべて最新状態で評価)", size=0,
    chart={"xAxis": "ReleaseMonth", "yAxis": ["PatchRate"], "group": "Site", "createOtherGroup": None,
           "ySettings": {"min": 0, "max": 100}},
))
items.append(kql(
    COUNTS, viz="table", name="monthly-counts", tab="counts", title="月次OSパッチの台数内訳",
    grid={
        "formatters": [BAR("Installed"), BAR("NotInstalled"), BAR("Unknown"), RATE_FORMATTER],
        "labelSettings": labels(
            Month="月次OSパッチ", Site="デバイスグループ", Targets="対象端末数",
            Installed="適用済み", NotInstalled="未適用", Unknown="未評価",
            PatchRate="適用率 (%)", Coverage="評価済み率 (%)",
        ),
    },
))
items.append(kql(
    BASELINE_DETAILS, viz="table", name="monthly-baselines", tab="detail",
    title="選択月の月例セキュリティKB・対象ビルド",
    grid={"labelSettings": labels(
        Month="公開月", Family="OS", BaseVersion="ビルド系列", PatchId="月例KB",
        RequiredFullVersion="適用基準ビルド", ReleaseDate="公開日", SourceUrl="Microsoft公式情報",
    )},
))

items.append(text(
    "### 端末別の月次パッチ到達状況\n"
    "適用済み端末も含む全対象端末を表示します。最新の `DeviceInfo` の "
    "`OSVersion`・`OSBuild`・`OsBuildRevision` からフルビルドを作り、"
    "Graphの月例セキュリティ更新に照合します。\n\n"
    "**到達済み月次パッチ**は、同じOS・ビルド系列で現在ビルドが満たす最新の月例更新です。"
    "表示月数・比較対象月より前後の更新も含め、カタログ全体から確認します。"
    "プレビュー・緊急累積更新も、それ以前の月例基準を満たせば到達済みとします。"
    "ビルド不明・Graph対応未確認は未評価とし、TVMインベントリでは補完しません。"
    "「確認する月次パッチ」は比較対象月の判定に使用します。",
    "device-updates-note", tab="devices",
))
items.append(kql(
    DEVICE_DETAILS, viz="table", name="monthly-devices", tab="devices",
    title="端末ビルドと到達済み月次パッチ",
    grid={
        "filter": True,
        "labelSettings": labels(
            Site="デバイスグループ", DeviceName="端末名", DeviceType="種別", OSPlatform="OS",
            FullVersion="DeviceInfoのフルビルド", AppliedMonth="到達済み月次パッチ",
            AppliedPatchId="到達済み月例KB", AppliedFullVersion="到達済み月例基準ビルド",
            TargetMonth="比較対象月", Status="比較対象月の状況", Reason="判定補足",
            LastSeen="DeviceInfoの最終確認",
        ),
    },
))

items.append(kql(
    EVALUATION + """DeviceState
| extend OS = strcat(OSPlatform, ' / ', iff(VersionCount == 1, FullVersion, '未評価'))
| summarize Devices = count() by Site, OS
| sort by Site asc""",
    viz="barchart", name="osver-dist", tab="osver", title="拠点別 Windows フルビルド分布", size=0,
    chart={"xAxis": "Site", "yAxis": ["Devices"], "group": "OS", "createOtherGroup": 15},
))
items.append(kql(
    EVALUATION + """DeviceState
| summarize Devices = count() by Site, OSPlatform, FullVersion, InventoryReason
| sort by Site asc, OSPlatform asc""",
    viz="table", name="osver-validation", tab="osver", title="Windows インベントリ検証",
    grid={"labelSettings": labels(
        Site="デバイスグループ", OSPlatform="OS", FullVersion="フルビルド",
        InventoryReason="判定できない理由", Devices="端末数",
    )},
))

items.append(text(
    "### データ診断\n"
    "Windows 10/11を対象に、`DeviceInfo`、`DeviceWindowsOsInventory_CL`、`WindowsMonthlyPatchCatalog_CL` を使用します。"
    "後者2表は Logic App と Automation Runbook による日次収集です。"
    "Snapshot 行は全件送信完了の印です。新しい取込が失敗した場合、途中のデータを適用判定に使いません。"
    "旧 `DeviceTvmOsPatch_CL` の脆弱性情報と、移行前のWindows Server履歴は、この適用率計算には使いません。",
    "diag-note", tab="diag",
))
items.append(kql(
    f"""DeviceInfo
| where TimeGenerated > ago(30d)
| project TimeGenerated, DeviceId, OnboardingStatus, DeviceType, OSPlatform, MachineGroup
| summarize arg_max(TimeGenerated, *) by DeviceId
| where {WINDOWS_CONDITION}
| summarize Devices = count(), LastRecord = max(TimeGenerated) by OnboardingStatus, DeviceType, OSPlatform, MachineGroup
| sort by Devices desc""",
    viz="table", name="diag-deviceinfo", tab="diag", title="DeviceInfo (過去30日・端末ごと最新)", size=0,
))
items.append(kql(
    """DeviceWindowsOsInventory_CL
| summarize Rows = count(), Devices = dcountif(DeviceId, RecordType == 'Device'),
            First = min(TimeGenerated), Last = max(TimeGenerated) by RecordType""",
    viz="table", name="diag-inventory-range", tab="diag",
    title="Windows フルビルドの取込範囲", size=4,
))
items.append(kql(
    """DeviceWindowsOsInventory_CL
| where TimeGenerated > ago(30d) and RecordType == 'Device'
| summarize Rows = count(), Devices = dcount(DeviceId) by Day = bin(TimeGenerated, 1d)
| sort by Day asc""",
    viz="barchart", name="diag-inventory-daily", tab="diag",
    title="Windows インベントリ日次取込 (過去30日)", size=1,
))
items.append(kql(
    SOURCES + """PublishedBuilds
| summarize Builds = count(), LatestMonthlyRelease = maxif(ReleaseMonth, ReleaseKind == 'B')
  by Family
| sort by Family asc""",
    viz="table", name="diag-catalog-coverage", tab="diag", title="Microsoft Graph対応表の収集範囲", size=0,
))

tab_items = {}
ungrouped = []
for item in items:
    visibility = item.get("conditionalVisibility")
    if visibility and visibility["parameterName"] == "Tab":
        item.pop("conditionalVisibility")
        tab_items.setdefault(visibility["value"], []).append(item)
    else:
        ungrouped.append(item)
for link in tabs["content"]["links"]:
    tab = link["subTarget"]
    ungrouped.append({
        "type": 12,
        "content": {
            "version": "NotebookGroup/1.0", "groupType": "editable",
            "loadType": "lazy", "items": tab_items[tab],
        },
        "conditionalVisibility": {
            "parameterName": "Tab", "comparison": "isEqualTo", "value": tab,
        },
        "name": f"tab-{tab}-group",
    })
items = ungrouped

workbook = {
    "version": "Notebook/1.0",
    "items": items,
    "fallbackResourceIds": [],
    "$schema": "https://github.com/Microsoft/Application-Insights-Workbooks/blob/master/schema/workbook.json",
}

with open(os.path.join(HERE, "YmcOSPatchStatus.workbook.json"), "w", encoding="utf-8") as f:
    json.dump(workbook, f, ensure_ascii=False, indent=2)

arm_workbook = copy.deepcopy(workbook)
arm_workbook["fallbackResourceIds"] = ["__WORKSPACE_RESOURCE_ID__"]
for parameter in arm_workbook["items"][1]["content"]["parameters"]:
    if parameter["name"] == "Workspace":
        parameter["value"] = "__WORKSPACE_RESOURCE_ID__"

arm = {
    "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
    "contentVersion": "1.0.0.0",
    "parameters": {
        "workbookDisplayName": {"type": "string", "defaultValue": "月次OSパッチ適用状況 (Win10/11・MDE 拠点別)"},
        "workbookSourceId": {
            "type": "string",
            "metadata": {"description": "Resource ID of the Log Analytics workspace (Sentinel) the workbook is attached to."},
        },
        "workbookId": {
            "type": "string",
            "defaultValue": "[guid(toLower(parameters('workbookSourceId')), 'YmcMonthlyOsPatch')]",
        },
        "location": {"type": "string", "defaultValue": "[resourceGroup().location]"},
    },
    "variables": {"workbookContent": json.dumps(arm_workbook, ensure_ascii=False)},
    "resources": [
        {
            "type": "Microsoft.Insights/workbooks",
            "apiVersion": "2022-04-01",
            "name": "[parameters('workbookId')]",
            "location": "[parameters('location')]",
            "kind": "shared",
            "properties": {
                "displayName": "[parameters('workbookDisplayName')]",
                "serializedData": "[replace(variables('workbookContent'), '__WORKSPACE_RESOURCE_ID__', parameters('workbookSourceId'))]",
                "version": "1.0",
                "sourceId": "[parameters('workbookSourceId')]",
                "category": "sentinel",
            },
        }
    ],
    "outputs": {
        "workbookId": {"type": "string", "value": "[parameters('workbookId')]"},
        "workbookResourceId": {
            "type": "string",
            "value": "[resourceId('Microsoft.Insights/workbooks', parameters('workbookId'))]",
        },
    },
}
with open(os.path.join(HERE, "YmcOSPatchStatus.arm.json"), "w", encoding="utf-8") as f:
    json.dump(arm, f, ensure_ascii=False, indent=2)

print("items:", len(items))
