"""Shared KQL for monthly Windows cumulative-update compliance."""

WINDOWS_CONDITION = "OSPlatform in ('Windows10', 'Windows11')"


def flt(column, parameter):
    values = "dynamic([{" + parameter + "}])"
    return f"(array_length({values}) == 0 or '*' in ({values}) or {column} in ({values}))"


DEVICES = f"""let Devices = DeviceInfo
| where TimeGenerated > ago(14d)
| project TimeGenerated, DeviceId, DeviceName, MachineGroup, OSPlatform, OSBuild,
          OSVersion = tostring(column_ifexists('OSVersion', '')),
          OsBuildRevision = tostring(column_ifexists('OsBuildRevision', '')),
          DeviceType, OnboardingStatus
| summarize arg_max(TimeGenerated, *) by DeviceId
| where {WINDOWS_CONDITION}
| where {flt("OnboardingStatus", "OnbSel")}
| extend Site = iff(isempty(MachineGroup), '(未割当)', MachineGroup)
| where {flt("Site", "SiteSel")}
| where {flt("OSPlatform", "OSSel")}
| where {flt("DeviceType", "TypeSel")}
| project DeviceId, DeviceName, Site, OSPlatform, OSBuild = tolong(OSBuild),
          OSVersion, OsBuildRevision, DeviceType, LastSeen = TimeGenerated, Family = tolower(OSPlatform);
"""

CATALOG_SOURCE = r"""let CatalogMetadata = toscalar(WindowsMonthlyPatchCatalog_CL
| where TimeGenerated > ago(14d) and RecordType == 'Snapshot'
| top 1 by TimeGenerated desc
| project Metadata = bag_pack('SnapshotId', SnapshotId, 'TimeGenerated', TimeGenerated));
let CatalogSnapshotId = tostring(CatalogMetadata.SnapshotId);
let CatalogTime = todatetime(CatalogMetadata.TimeGenerated);
let CatalogFresh = isnotnull(CatalogTime) and CatalogTime > ago(2d) and CatalogTime <= now() + 5m;
let PublishedBuilds = WindowsMonthlyPatchCatalog_CL
| where TimeGenerated > ago(14d) and RecordType == 'Build' and SnapshotId == CatalogSnapshotId
| where ReleaseDate <= now()
| project Family = tolower(Family), BaseVersion, FullVersion, Revision, ReleaseMonth,
          ReleaseDate, PatchId, ReleaseKind, SourceUrl
| where Family in ('windows10', 'windows11');
let KnownBuilds = PublishedBuilds
| summarize by Family, FullVersion
| extend KnownPublishedBuild = 1;
let Baselines = PublishedBuilds
| where ReleaseKind == 'B'
| summarize arg_max(Revision, *) by Family, BaseVersion, ReleaseMonth
| project Family, BaseVersion, ReleaseMonth, RequiredRevision = Revision,
          RequiredFullVersion = FullVersion, PatchId, ReleaseDate, SourceUrl;
let ShownMonths = Baselines
| summarize by ReleaseMonth
| top toint('{MonthCount}') by ReleaseMonth desc;
"""

INVENTORY_SOURCE = r"""let InventoryMetadata = toscalar(DeviceWindowsOsInventory_CL
| where TimeGenerated > ago(14d) and RecordType == 'Snapshot'
| top 1 by TimeGenerated desc
| project Metadata = bag_pack('SnapshotId', SnapshotId, 'TimeGenerated', TimeGenerated));
let InventorySnapshotId = tostring(InventoryMetadata.SnapshotId);
let InventoryTime = todatetime(InventoryMetadata.TimeGenerated);
let InventoryFresh = isnotnull(InventoryTime) and InventoryTime > ago(2d) and InventoryTime <= now() + 5m;
let Inventory = DeviceWindowsOsInventory_CL
| where TimeGenerated > ago(14d) and RecordType == 'Device' and SnapshotId == InventorySnapshotId
| summarize Versions = make_set(SoftwareVersion, 16), Platforms = make_set(OSPlatform, 16) by DeviceId
| project InventoryDeviceId = DeviceId, Versions, Platforms;
"""

SOURCES = CATALOG_SOURCE + INVENTORY_SOURCE

SELECTED_MONTH = r"""let SelectedMonth = iff('{MonthSel}' == 'latest',
    toscalar(ShownMonths | summarize max(ReleaseMonth)), todatetime('{MonthSel}'));
"""

def option_query(column):
    return f"""DeviceInfo
| where TimeGenerated > ago(14d)
| project OSPlatform, MachineGroup, OnboardingStatus, DeviceType
| where {WINDOWS_CONDITION}
| extend Site = iff(isempty(MachineGroup), '(未割当)', MachineGroup)
| where isnotempty({column})
| summarize by value = tostring({column})
| project value, label = value
| sort by value asc
"""


# Dropdown candidates: one value/label query per filter parameter.
OPTION_QUERIES = {
    "SiteSel": option_query("Site"),
    "OSSel": option_query("OSPlatform"),
    "OnbSel": option_query("OnboardingStatus"),
    "TypeSel": option_query("DeviceType"),
}

EVALUATION = DEVICES + SOURCES + r"""let DeviceState = Devices
| join kind=leftouter Inventory on $left.DeviceId == $right.InventoryDeviceId
| extend VersionCount = array_length(Versions), PlatformCount = array_length(Platforms),
         FullVersion = tostring(Versions[0]), InventoryPlatform = tolower(tostring(Platforms[0]))
| extend BaseVersion = extract(@"^(\d+\.\d+\.\d+)\.\d+$", 1, FullVersion),
         CurrentRevision = tolong(extract(@"^\d+\.\d+\.\d+\.(\d+)$", 1, FullVersion)),
         CurrentBuild = tolong(extract(@"^\d+\.\d+\.(\d+)\.\d+$", 1, FullVersion))
| lookup kind=leftouter KnownBuilds on Family, FullVersion
| extend InventoryReason = case(
    not(CatalogFresh), '月例カタログ未取込・期限切れ',
    not(InventoryFresh), 'OSインベントリ未取込・期限切れ',
    isempty(InventoryDeviceId), 'OSインベントリなし',
    VersionCount != 1, 'OSバージョンが一意でない',
    PlatformCount != 1 or InventoryPlatform != Family, 'OSプラットフォーム不一致',
    isempty(BaseVersion) or isnull(CurrentRevision), 'フルビルド番号不明',
    isnull(OSBuild) or OSBuild != CurrentBuild, 'DeviceInfoとTVMのビルド不一致',
    isnull(KnownPublishedBuild), 'Microsoft公開ビルドとの対応未確認',
    '');
let Evaluations = DeviceState
| extend CrossKey = 1
| join kind=inner (ShownMonths | extend CrossKey = 1) on CrossKey
| lookup kind=leftouter Baselines on Family, BaseVersion, ReleaseMonth
| extend Reason = iff(isnotempty(InventoryReason), InventoryReason,
                      iff(isnull(RequiredRevision), '当月の対応する月例更新が未確認', ''))
| extend Status = case(isnotempty(Reason), 'Unknown',
                       CurrentRevision >= RequiredRevision, 'Installed', 'NotInstalled');
"""

MONTHLY_SUMMARY = EVALUATION + r"""let MonthlySummary = Evaluations
| summarize Targets = count(), Installed = countif(Status == 'Installed'),
            NotInstalled = countif(Status == 'NotInstalled'), Unknown = countif(Status == 'Unknown')
  by ReleaseMonth, Site
| extend Assessed = Installed + NotInstalled
| extend PatchRate = iff(Targets > 0 and Assessed > 0, round(100.0 * Installed / Targets, 1), real(null)),
         Coverage = iff(Targets > 0, round(100.0 * Assessed / Targets, 1), real(null));
"""

MATRIX = MONTHLY_SUMMARY + r"""MonthlySummary
| extend MonthLabel = format_datetime(ReleaseMonth, 'yyyy/MM'),
         Rate = case(Targets == 0, 'N/A', Assessed == 0, '未評価', strcat(tostring(PatchRate), '%'))
| evaluate pivot(Site, take_any(Rate), MonthLabel)
| sort by MonthLabel asc"""

UNKNOWN_MATRIX = MONTHLY_SUMMARY + r"""MonthlySummary
| extend MonthLabel = format_datetime(ReleaseMonth, 'yyyy/MM')
| evaluate pivot(Site, sum(Unknown), MonthLabel)
| sort by MonthLabel asc"""

COUNTS = MONTHLY_SUMMARY + r"""MonthlySummary
| project Month = format_datetime(ReleaseMonth, 'yyyy/MM'), Site, Targets, Installed,
          NotInstalled, Unknown, PatchRate, Coverage
| sort by Month asc, Site asc"""

STATUS = DEVICES + SOURCES + r"""print
    InventoryLast = InventoryTime, CatalogLast = CatalogTime,
    TargetDevices = toscalar(Devices | count),
    LatestPatchMonth = toscalar(ShownMonths | summarize max(ReleaseMonth)),
    IngestionState = case(not(CatalogFresh), '要確認: 月例カタログ未取込・2日超過',
                          not(InventoryFresh), '要確認: OSインベントリ未取込・2日超過',
                          '取込済み: 未評価台数は台数内訳で確認')"""

TILES = MONTHLY_SUMMARY + SELECTED_MONTH + r"""MonthlySummary
| where ReleaseMonth == SelectedMonth
| summarize Targets = sum(Targets), Installed = sum(Installed),
            NotInstalled = sum(NotInstalled), Unknown = sum(Unknown), Assessed = sum(Assessed)
| extend Rate = case(Targets == 0, '対象なし', Assessed == 0, '未評価',
                     strcat(tostring(round(100.0 * Installed / Targets, 1)), '%'))
| project a = pack_array(
    bag_pack('Order', 1, 'Label', '対象端末数', 'Value', tostring(Targets)),
    bag_pack('Order', 2, 'Label', '適用済み端末数', 'Value', tostring(Installed)),
    bag_pack('Order', 3, 'Label', '未適用端末数', 'Value', tostring(NotInstalled)),
    bag_pack('Order', 4, 'Label', '未評価端末数', 'Value', tostring(Unknown)),
    bag_pack('Order', 5, 'Label', '確認済み適用率', 'Value', Rate))
| mv-expand a
| project Order = toint(a.Order), Label = tostring(a.Label), Value = tostring(a.Value)
| sort by Order asc
| project Label, Value"""

MONTH_OPTIONS = CATALOG_SOURCE + r"""union
    (print value = 'latest', label = '最新の公開月'),
    (ShownMonths | project value = format_datetime(ReleaseMonth, 'yyyy-MM-dd'),
                          label = format_datetime(ReleaseMonth, 'yyyy/MM'))
| project value, label"""

BASELINE_DETAILS = SOURCES + SELECTED_MONTH + r"""Baselines
| where ReleaseMonth == SelectedMonth
| project Month = format_datetime(ReleaseMonth, 'yyyy/MM'), Family, BaseVersion,
          PatchId, RequiredFullVersion, ReleaseDate, SourceUrl
| sort by Family asc, BaseVersion asc"""

DEVICE_DETAILS = DEVICES + CATALOG_SOURCE + SELECTED_MONTH + r"""let DeviceBuildState = Devices
| extend CurrentRevision = tolong(OsBuildRevision),
         BaseVersion = iff(OSVersion matches regex @"^\d+\.\d+$" and OSBuild > 0,
                           strcat(OSVersion, '.', tostring(OSBuild)), '')
| extend FullVersion = iff(isnotempty(BaseVersion) and isnotnull(CurrentRevision)
                           and OsBuildRevision matches regex @"^\d+$",
                           strcat(BaseVersion, '.', tostring(CurrentRevision)), '')
| lookup kind=leftouter KnownBuilds on Family, FullVersion
| extend BuildReason = case(
    not(CatalogFresh), '月例カタログ未取込・期限切れ',
    LastSeen > now() + 5m, 'DeviceInfoの確認日時が未来',
    isempty(BaseVersion), 'DeviceInfoのOSVersion/OSBuildが不明・不正',
    isempty(FullVersion), 'DeviceInfoのOsBuildRevisionが不明・不正',
    isnull(KnownPublishedBuild), 'Graph公開ビルドとの対応未確認',
    '');
DeviceBuildState
| join kind=leftouter (
    Baselines
    | project Family, BaseVersion, MonthlyRelease = ReleaseMonth, MonthlyRevision = RequiredRevision,
              MonthlyPatchId = PatchId, MonthlyFullVersion = RequiredFullVersion
) on Family, BaseVersion
| extend ReachedMonth = iff(isempty(BuildReason) and CurrentRevision >= MonthlyRevision,
                            MonthlyRelease, datetime(null))
| summarize arg_max(ReachedMonth, MonthlyPatchId, MonthlyFullVersion),
            TargetBaselines = countif(MonthlyRelease == SelectedMonth),
            TargetRevision = take_anyif(MonthlyRevision, MonthlyRelease == SelectedMonth)
  by DeviceId, Site, DeviceName, DeviceType, OSPlatform, FullVersion, BuildReason,
     CurrentRevision, LastSeen
| extend AppliedMonth = case(isnotempty(BuildReason), '未評価',
                              isnull(ReachedMonth), '該当する月例更新なし',
                              format_datetime(ReachedMonth, 'yyyy/MM')),
         AppliedPatchId = iff(isnull(ReachedMonth), '', MonthlyPatchId),
         AppliedFullVersion = iff(isnull(ReachedMonth), '', MonthlyFullVersion),
         Reason = case(isnotempty(BuildReason), BuildReason,
                       isnull(SelectedMonth), '比較対象月が不明',
                       TargetBaselines != 1, '当月の対応する月例更新が未確認', '')
| extend Status = case(isnotempty(Reason), '未評価',
                       CurrentRevision >= TargetRevision, '対象月まで更新済み', '対象月未到達')
| project Site, DeviceName, DeviceType, OSPlatform, FullVersion, AppliedMonth, AppliedPatchId,
          AppliedFullVersion, TargetMonth = format_datetime(SelectedMonth, 'yyyy/MM'),
          Status, Reason, LastSeen
| sort by Site asc, DeviceName asc"""
