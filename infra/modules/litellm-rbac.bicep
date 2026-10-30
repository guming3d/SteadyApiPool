// Least-privilege custom role for the pool manager on one LiteLLM gateway on Azure Container
// Apps. Reading the Container App gives the gateway's URL and identity, and listSecrets its
// master key: every pool change then goes through LiteLLM's admin API, not ARM. The logs read on
// the gateway's Application Insights component feeds the Monitor tab, and the Container App's
// metrics the Gateway metrics tab (its database's need Monitoring Reader there). The app can't change the
// Container App, its environment, database or anything else here. Mirrors GATEWAY_ACTIONS in
// backend/pool_manager/access.py.
targetScope = 'resourceGroup'

param gatewayName string
param principalId string

@description('The gateway\'s Application Insights component. Empty skips the Monitor tab\'s gateway view.')
param appInsightsName string = 'appi-${gatewayName}'

resource gateway 'Microsoft.App/containerApps@2025-01-01' existing = {
  name: gatewayName
}

resource insights 'Microsoft.Insights/components@2020-02-02' existing = if (!empty(appInsightsName)) {
  name: appInsightsName
}

resource poolManagerRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(resourceGroup().id, 'ai-gateway-pool-manager')
  properties: {
    roleName: 'AI Gateway Pool Manager ${uniqueString(resourceGroup().id)}'
    description: 'Reads a LiteLLM gateway\'s Container App and master key, and its Application Insights logs, for SteadyApiPool.'
    type: 'CustomRole'
    permissions: [
      {
        actions: [
          'Microsoft.App/containerApps/read'
          'Microsoft.App/containerApps/listSecrets/action'
          // The setup wizard checks the app's own effective permissions.
          'Microsoft.Authorization/permissions/read'
          // The Monitor tab queries the gateway's OpenTelemetry spans in resource context.
          'Microsoft.Insights/components/read'
          'Microsoft.Insights/logs/*/read'
          // The Gateway metrics tab reads the Container App's replicas, CPU and memory.
          'Microsoft.Insights/metrics/read'
        ]
        notActions: []
        dataActions: []
        notDataActions: []
      }
    ]
    assignableScopes: [
      resourceGroup().id
    ]
  }
}

resource gatewayAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(gateway.id, principalId, 'ai-gateway-pool-manager')
  scope: gateway
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: poolManagerRole.id
  }
}

resource insightsAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(appInsightsName)) {
  name: guid(insights.id, principalId, 'ai-gateway-pool-manager')
  scope: insights
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: poolManagerRole.id
  }
}
