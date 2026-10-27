// Read-only access for deployment discovery: list Azure OpenAI / Foundry accounts and their
// deployments in one subscription. It deliberately excludes listKeys; discovered members
// authenticate with the gateway's managed identity instead of account keys.
targetScope = 'subscription'

param principalId string

@description('Name of the role, before a suffix unique to the subscription. Each product keeps its own.')
param roleName string

@description('What the GUID names of the role and its assignment are made from; each product keeps its own.')
param seed string

resource discoveryRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(subscription().id, seed)
  properties: {
    roleName: '${roleName} ${uniqueString(subscription().id)}'
    description: 'Lists Cognitive Services accounts and model deployments for SteadyApiPool.'
    type: 'CustomRole'
    permissions: [
      {
        actions: [
          'Microsoft.CognitiveServices/accounts/read'
          'Microsoft.CognitiveServices/accounts/deployments/read'
          // The setup wizard checks the app's permissions here, and whether the gateway's identity
          // has an inference role on each Foundry account. Reading roles cannot change them.
          'Microsoft.Authorization/permissions/read'
          'Microsoft.Authorization/roleAssignments/read'
          'Microsoft.Authorization/roleDefinitions/read'
          // The pool dashboard reads each member deployment's Azure OpenAI metrics.
          'Microsoft.Insights/metrics/read'
        ]
        notActions: []
        dataActions: []
        notDataActions: []
      }
    ]
    assignableScopes: [
      subscription().id
    ]
  }
}

resource assignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(subscription().id, principalId, seed)
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: discoveryRole.id
  }
}
