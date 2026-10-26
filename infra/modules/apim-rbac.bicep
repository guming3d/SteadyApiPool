// Least-privilege custom role for the pool manager on one APIM service. It can manage backends,
// named values and API-level policies, read APIs, and create the HTTP API (with one wildcard
// operation) that a new pool is published on. The app only writes and deletes APIs it created itself (it deletes
// them when their pool is removed). It cannot change products, subscriptions or the service itself.
targetScope = 'resourceGroup'

param apimName string
param principalId string

resource apim 'Microsoft.ApiManagement/service@2024-05-01' existing = {
  name: apimName
}

resource poolManagerRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(resourceGroup().id, 'apim-backend-pool-manager')
  properties: {
    roleName: 'APIM Backend Pool Manager ${uniqueString(resourceGroup().id)}'
    description: 'Manages APIM backends and backend pools, their credential named values, API-level backend policies, and the APIs it creates for pools.'
    type: 'CustomRole'
    permissions: [
      {
        actions: [
          'Microsoft.ApiManagement/service/read'
          'Microsoft.ApiManagement/service/backends/read'
          'Microsoft.ApiManagement/service/backends/write'
          'Microsoft.ApiManagement/service/backends/delete'
          'Microsoft.ApiManagement/service/namedValues/read'
          'Microsoft.ApiManagement/service/namedValues/write'
          'Microsoft.ApiManagement/service/namedValues/delete'
          'Microsoft.ApiManagement/service/apis/read'
          'Microsoft.ApiManagement/service/apis/write'
          'Microsoft.ApiManagement/service/apis/delete'
          'Microsoft.ApiManagement/service/apis/operations/write'
          'Microsoft.ApiManagement/service/apis/policies/read'
          'Microsoft.ApiManagement/service/apis/policies/write'
          // The setup wizard checks the app's own effective permissions.
          'Microsoft.Authorization/permissions/read'
          // The pool dashboard reads the gateway's Azure Monitor metrics.
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
  name: guid(apim.id, principalId, 'apim-backend-pool-manager')
  scope: apim
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: poolManagerRole.id
  }
}
