// The guest view reads the demo APIM service and the Foundry deployments in its subscription with
// the app's managed identity. Reader can read them (and their metrics) but change nothing, and it
// has no listSecrets or listKeys, so subscription keys and named-value secrets stay out of reach.
targetScope = 'subscription'

param principalId string

var readerRole = 'acdd72a7-3385-48ef-bd42-f606fba81ae7'

resource reader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(subscription().id, principalId, readerRole)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', readerRole)
    principalId: principalId
    principalType: 'ServicePrincipal'
    description: 'SteadyApiPool (public): read-only guest view'
  }
}
