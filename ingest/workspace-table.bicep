targetScope = 'resourceGroup'

param workspaceName string
param tableName string

type Column = {
  name: string
  type: 'string' | 'datetime' | 'int' | 'long' | 'real'
}

param columns Column[]

resource workspace 'Microsoft.OperationalInsights/workspaces@2022-10-01' existing = {
  name: workspaceName
}

resource table 'Microsoft.OperationalInsights/workspaces/tables@2022-10-01' = {
  parent: workspace
  name: tableName
  properties: {
    schema: {
      name: tableName
      columns: concat([
        { name: 'TimeGenerated', type: 'datetime' }
      ], columns)
    }
    retentionInDays: 365
    totalRetentionInDays: 365
  }
}
