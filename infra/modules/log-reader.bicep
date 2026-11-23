// Log Analytics Reader for the pool manager on one workspace: the Monitor tab reads an AI
// Gateway's OTelLogs and OTelSpans there.
targetScope = 'resourceGroup'

param workspaceName string
param principalId string

// Built-in Log Analytics Reader.
var logAnalyticsReader = '73c42c96-874c-492b-b04d-ab87d138a893'

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: workspaceName
}

resource assignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(workspace.id, principalId, logAnalyticsReader)
  scope: workspace
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', logAnalyticsReader)
  }
}
