// Windows 月例更新カタログ (Graph) を取得し、WindowsMonthlyPatchCatalog_CL へ取り込む Logic App。
targetScope = 'resourceGroup'

param logicAppName string = 'logic-windows-patch-catalog-ingest'
param location string = resourceGroup().location

@description('既存の DCE / DCR (同じリソースグループ)')
param dceName string
param dcrName string

@description('実行時刻 (日本時間の時)。インベントリ取得と時間をずらす')
param runHourJst int = 3

@description('実行時刻 (日本時間の分)')
param runMinuteJst int = 30

@description('false で配置し、Graph 権限の付与後に有効化')
param enabled bool = true

var catalogStreamName = 'Custom-WindowsMonthlyPatchCatalog_CL'
var catalogActions = loadJsonContent('catalog-actions.json')

resource dce 'Microsoft.Insights/dataCollectionEndpoints@2023-03-11' existing = {
  name: dceName
}

resource dcr 'Microsoft.Insights/dataCollectionRules@2023-03-11' existing = {
  name: dcrName
}

resource logicApp 'Microsoft.Logic/workflows@2019-05-01' = {
  name: logicAppName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    state: enabled ? 'Enabled' : 'Disabled'
    definition: {
      '$schema': 'https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json#'
      contentVersion: '1.0.0.0'
      parameters: {
        CatalogIngestionUri: {
          type: 'String'
          defaultValue: '${dce.properties.logsIngestion.endpoint}/dataCollectionRules/${dcr.properties.immutableId}/streams/${catalogStreamName}?api-version=2023-01-01'
        }
      }
      triggers: {
        Daily: {
          type: 'Recurrence'
          recurrence: {
            frequency: 'Day'
            interval: 1
            timeZone: 'Tokyo Standard Time'
            schedule: {
              hours: [runHourJst]
              minutes: [runMinuteJst]
            }
          }
        }
      }
      actions: catalogActions
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

output catalogLogicAppName string = logicApp.name
output catalogLogicAppPrincipalId string = logicApp.identity.principalId
