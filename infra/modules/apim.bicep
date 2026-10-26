// A new API Management service for the pool manager, used when no existing service is given.
// v2 tiers are created in minutes; Developer takes 30-40 minutes and has no SLA. Consumption is
// not offered because it has no backend circuit breaker, which imported members use.
targetScope = 'resourceGroup'

param name string
param location string
param tags object

@allowed([ 'BasicV2', 'StandardV2', 'Developer' ])
param skuName string

@description('Receives APIM system notifications.')
param publisherEmail string
param publisherName string

resource apim 'Microsoft.ApiManagement/service@2024-05-01' = {
  name: name
  location: location
  // Same tag as services created from the local setup wizard.
  tags: union(tags, { 'managed-by': 'apim-backend-pool' })
  sku: { name: skuName, capacity: 1 }
  // Pool members authenticate to Azure OpenAI / Foundry with this identity.
  identity: { type: 'SystemAssigned' }
  properties: {
    publisherEmail: publisherEmail
    publisherName: publisherName
  }
}

output name string = apim.name
output principalId string = apim.identity.principalId
