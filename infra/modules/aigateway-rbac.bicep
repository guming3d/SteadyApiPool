// Least-privilege custom role for the pool manager on one Azure AI Gateway. It can manage model
// providers, models and aliases in the default workspace (the app only changes or deletes those it
// created), read the telemetry exporters, and delete the one test key it creates. It cannot
// read or create runtime keys (the signed-in user does that with their own rights), change the
// gateway itself, or call models. Mirrors AIGATEWAY_READ_ACTIONS and AIGATEWAY_WRITE_ACTIONS in
// backend/pool_manager/aigateway/access.py.
targetScope = 'resourceGroup'

param gatewayName string
param principalId string

#disable-next-line BCP081
resource gateway 'Microsoft.ApiManagement/aigateways@2026-11-01' existing = {
  name: gatewayName
}

resource poolManagerRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(resourceGroup().id, 'steadyapipool-aigateway-manager')
  properties: {
    roleName: 'SteadyApiPool AI Gateway Manager ${uniqueString(resourceGroup().id)}'
    description: 'Manages the model providers, models and aliases SteadyApiPool creates on an Azure AI Gateway.'
    type: 'CustomRole'
    permissions: [
      {
        actions: [
          'Microsoft.ApiManagement/aigateways/read'
          'Microsoft.ApiManagement/aigateways/workspaces/read'
          'Microsoft.ApiManagement/aigateways/workspaces/aliases/read'
          'Microsoft.ApiManagement/aigateways/workspaces/aliases/write'
          'Microsoft.ApiManagement/aigateways/workspaces/aliases/delete'
          'Microsoft.ApiManagement/aigateways/workspaces/modelProviders/read'
          'Microsoft.ApiManagement/aigateways/workspaces/modelProviders/write'
          'Microsoft.ApiManagement/aigateways/workspaces/modelProviders/delete'
          'Microsoft.ApiManagement/aigateways/workspaces/modelProviders/models/read'
          'Microsoft.ApiManagement/aigateways/workspaces/modelProviders/models/write'
          'Microsoft.ApiManagement/aigateways/workspaces/modelProviders/models/delete'
          'Microsoft.ApiManagement/aigateways/workspaces/models/read'
          'Microsoft.ApiManagement/aigateways/workspaces/telemetryExporters/read'
          // Only the test key this app created, when the gateway is cleaned up.
          'Microsoft.ApiManagement/aigateways/apiKeys/delete'
          // The setup wizard checks the app's own effective permissions.
          'Microsoft.Authorization/permissions/read'
          // The Monitor tab finds the workspace behind the exporter's Application Insights.
          'Microsoft.Insights/components/read'
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

resource assignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(gateway.id, principalId, 'steadyapipool-aigateway-manager')
  scope: gateway
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: poolManagerRole.id
  }
}
