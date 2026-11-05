// Defender の Windows OS インベントリ (TVM) と月例更新カタログを、別々の Logic App で毎日取得する。
targetScope = 'resourceGroup'

@description('Sentinel が有効な既存の Log Analytics ワークスペース名')
param workspaceName string

@description('既存ワークスペースのリソースグループ名 (同一サブスクリプション。省略時はデプロイ先)')
param workspaceResourceGroupName string = resourceGroup().name

@description('Logic App のリージョン (DCE / DCR の既定値にも使用)')
param location string = resourceGroup().location

@description('DCE / DCR のリージョン。既存ワークスペースのリージョンに必ず合わせる')
param dataCollectionLocation string = location

param logicAppName string = 'logic-defender-os-patch-ingest'
param catalogLogicAppName string = 'logic-windows-patch-catalog-ingest'
param dceName string = 'dce-defender-os-patch'
param dcrName string = 'dcr-defender-os-patch'

@description('初回は false で配置し、Graph 権限と取り込み設定を確認後に有効化 (両方の Logic App に適用)')
param enableIngestion bool = true

@description('インベントリ取得の実行時刻 (日本時間の時)')
param runHourJst int = 3

@description('カタログ取得の実行時刻 (日本時間の時・分)。インベントリ取得とずらす')
param catalogRunHourJst int = 3
param catalogRunMinuteJst int = 30

var tableName = 'DeviceTvmOsPatch_CL'
var streamName = 'Custom-${tableName}'
var inventoryTableName = 'DeviceWindowsOsInventory_CL'
var inventoryStreamName = 'Custom-${inventoryTableName}'
var catalogTableName = 'WindowsMonthlyPatchCatalog_CL'
var catalogStreamName = 'Custom-${catalogTableName}'

var legacyColumns = [
  { name: 'DeviceId', type: 'string' }
  { name: 'DeviceName', type: 'string' }
  { name: 'OSPlatform', type: 'string' }
  { name: 'SoftwareName', type: 'string' }
  { name: 'PatchId', type: 'string' }
  { name: 'UpdateName', type: 'string' }
  { name: 'VulnerabilitySeverityLevel', type: 'string' }
  { name: 'CveCount', type: 'int' }
  { name: 'MaxCvss', type: 'real' }
  { name: 'ExploitAvailable', type: 'int' }
]
var inventoryColumns = [
  { name: 'SnapshotId', type: 'string' }
  { name: 'SnapshotTime', type: 'datetime' }
  { name: 'RecordType', type: 'string' }
  { name: 'DeviceId', type: 'string' }
  { name: 'DeviceName', type: 'string' }
  { name: 'OSPlatform', type: 'string' }
  { name: 'SoftwareName', type: 'string' }
  { name: 'SoftwareVersion', type: 'string' }
  { name: 'OSVersion', type: 'string' }
  { name: 'OSArchitecture', type: 'string' }
  { name: 'ProductCodeCpe', type: 'string' }
]
var catalogColumns = [
  { name: 'SnapshotId', type: 'string' }
  { name: 'SnapshotTime', type: 'datetime' }
  { name: 'RecordType', type: 'string' }
  { name: 'Family', type: 'string' }
  { name: 'BaseVersion', type: 'string' }
  { name: 'FullVersion', type: 'string' }
  { name: 'Revision', type: 'long' }
  { name: 'ReleaseMonth', type: 'datetime' }
  { name: 'ReleaseDate', type: 'datetime' }
  { name: 'PatchId', type: 'string' }
  { name: 'ReleaseKind', type: 'string' }
  { name: 'SourceUrl', type: 'string' }
]

var inventoryQuery = '''
DeviceTvmSoftwareInventory
| where SoftwareVendor =~ "microsoft"
| where SoftwareName matches regex "^windows_(10|11)$"
| project DeviceId, DeviceName, OSPlatform, SoftwareName, SoftwareVersion, OSVersion,
          OSArchitecture, ProductCodeCpe, RecordType = "Device"
'''

resource workspace 'Microsoft.OperationalInsights/workspaces@2022-10-01' existing = {
  scope: resourceGroup(workspaceResourceGroupName)
  name: workspaceName
}

module table 'workspace-table.bicep' = {
  scope: resourceGroup(workspaceResourceGroupName)
  params: {
    workspaceName: workspaceName
    tableName: tableName
    columns: legacyColumns
  }
}

module inventoryTable 'workspace-table.bicep' = {
  scope: resourceGroup(workspaceResourceGroupName)
  params: {
    workspaceName: workspaceName
    tableName: inventoryTableName
    columns: inventoryColumns
  }
}

module catalogTable 'workspace-table.bicep' = {
  scope: resourceGroup(workspaceResourceGroupName)
  params: {
    workspaceName: workspaceName
    tableName: catalogTableName
    columns: catalogColumns
  }
}

resource dce 'Microsoft.Insights/dataCollectionEndpoints@2023-03-11' = {
  name: dceName
  location: dataCollectionLocation
  properties: {
    networkAcls: {
      publicNetworkAccess: 'Enabled'
    }
  }
}

resource dcr 'Microsoft.Insights/dataCollectionRules@2023-03-11' = {
  name: dcrName
  location: dataCollectionLocation
  dependsOn: [
    table
    inventoryTable
    catalogTable
  ]
  properties: {
    dataCollectionEndpointId: dce.id
    streamDeclarations: {
      '${streamName}': {
        columns: legacyColumns
      }
      '${inventoryStreamName}': {
        columns: inventoryColumns
      }
      '${catalogStreamName}': {
        columns: catalogColumns
      }
    }
    destinations: {
      logAnalytics: [
        {
          name: 'law'
          workspaceResourceId: workspace.id
        }
      ]
    }
    dataFlows: [
      {
        streams: [streamName]
        destinations: ['law']
        transformKql: 'source | extend TimeGenerated = now()'
        outputStream: streamName
      }
      {
        streams: [inventoryStreamName]
        destinations: ['law']
        transformKql: 'source | extend TimeGenerated = SnapshotTime'
        outputStream: inventoryStreamName
      }
      {
        streams: [catalogStreamName]
        destinations: ['law']
        transformKql: 'source | extend TimeGenerated = SnapshotTime'
        outputStream: catalogStreamName
      }
    ]
  }
}

resource logicApp 'Microsoft.Logic/workflows@2019-05-01' = {
  name: logicAppName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    state: enableIngestion ? 'Enabled' : 'Disabled'
    definition: {
      '$schema': 'https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json#'
      contentVersion: '1.0.0.0'
      triggers: {
        Daily: {
          type: 'Recurrence'
          recurrence: {
            frequency: 'Day'
            interval: 1
            timeZone: 'Tokyo Standard Time'
            schedule: {
              hours: [runHourJst]
              minutes: [0]
            }
          }
        }
      }
      actions: {
        Snapshot_Time: {
          type: 'Compose'
          inputs: '@utcNow()'
        }
        Run_Advanced_Hunting: {
          type: 'Http'
          runAfter: {
            Snapshot_Time: ['Succeeded']
          }
          inputs: {
            method: 'POST'
            uri: 'https://graph.microsoft.com/v1.0/security/runHuntingQuery'
            headers: {
              'Content-Type': 'application/json'
            }
            body: {
              Query: inventoryQuery
            }
            authentication: {
              type: 'ManagedServiceIdentity'
              audience: 'https://graph.microsoft.com/'
            }
            retryPolicy: {
              type: 'exponential'
              count: 3
              interval: 'PT1M'
            }
          }
        }
        Inventory_Rows: {
          type: 'Select'
          runAfter: {
            Run_Advanced_Hunting: ['Succeeded']
          }
          inputs: {
            from: '@body(\'Run_Advanced_Hunting\')?[\'results\']'
            select: {
              SnapshotId: '@workflow().run.name'
              SnapshotTime: '@outputs(\'Snapshot_Time\')'
              RecordType: '@item()[\'RecordType\']'
              DeviceId: '@item()[\'DeviceId\']'
              DeviceName: '@item()[\'DeviceName\']'
              OSPlatform: '@item()[\'OSPlatform\']'
              SoftwareName: '@item()[\'SoftwareName\']'
              SoftwareVersion: '@item()[\'SoftwareVersion\']'
              OSVersion: '@item()[\'OSVersion\']'
              OSArchitecture: '@item()[\'OSArchitecture\']'
              ProductCodeCpe: '@item()[\'ProductCodeCpe\']'
            }
          }
        }
        For_each_chunk: {
          type: 'Foreach'
          runAfter: {
            Inventory_Rows: ['Succeeded']
          }
          foreach: '@chunk(body(\'Inventory_Rows\'), 500)'
          actions: {
            Send_to_Log_Analytics: {
              type: 'Http'
              inputs: {
                method: 'POST'
                uri: '${dce.properties.logsIngestion.endpoint}/dataCollectionRules/${dcr.properties.immutableId}/streams/${inventoryStreamName}?api-version=2023-01-01'
                headers: {
                  'Content-Type': 'application/json'
                }
                body: '@items(\'For_each_chunk\')'
                authentication: {
                  type: 'ManagedServiceIdentity'
                  audience: 'https://monitor.azure.com'
                }
              }
            }
          }
          runtimeConfiguration: {
            concurrency: {
              repetitions: 1
            }
          }
        }
        Complete_Inventory_Snapshot: {
          type: 'Http'
          runAfter: { For_each_chunk: ['Succeeded'] }
          inputs: {
            method: 'POST'
            headers: { 'Content-Type': 'application/json' }
            uri: '${dce.properties.logsIngestion.endpoint}/dataCollectionRules/${dcr.properties.immutableId}/streams/${inventoryStreamName}?api-version=2023-01-01'
            body: [
              {
                SnapshotId: '@workflow().run.name'
                SnapshotTime: '@outputs(\'Snapshot_Time\')'
                RecordType: 'Snapshot'
                DeviceId: ''
                DeviceName: ''
                OSPlatform: ''
                SoftwareName: ''
                SoftwareVersion: ''
                OSVersion: ''
                OSArchitecture: ''
                ProductCodeCpe: ''
              }
            ]
            authentication: {
              type: 'ManagedServiceIdentity'
              audience: 'https://monitor.azure.com'
            }
          }
        }
      }
    }
  }
}

// Logic App -> DCR への取り込み権限 (Monitoring Metrics Publisher)
resource publisherRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: dcr
  name: guid(dcr.id, logicApp.id, 'MonitoringMetricsPublisher')
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '3913510d-42f4-4e42-8a64-420c390055eb'
    )
    principalId: logicApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// 月例更新カタログは別の Logic App で取得する (Graph 権限・実行時刻・障害を分離)
module catalogLogicApp 'catalog-logic-app.bicep' = {
  name: 'catalogLogicApp'
  dependsOn: [
    dce
    dcr
  ]
  params: {
    logicAppName: catalogLogicAppName
    location: location
    dceName: dceName
    dcrName: dcrName
    runHourJst: catalogRunHourJst
    runMinuteJst: catalogRunMinuteJst
    enabled: enableIngestion
  }
}

output logicAppPrincipalId string = logicApp.identity.principalId
output logicAppName string = logicApp.name
output catalogLogicAppPrincipalId string = catalogLogicApp.outputs.catalogLogicAppPrincipalId
output catalogLogicAppName string = catalogLogicApp.outputs.catalogLogicAppName
output logIngestionEndpoint string = dce.properties.logsIngestion.endpoint
output dcrImmutableId string = dcr.properties.immutableId
