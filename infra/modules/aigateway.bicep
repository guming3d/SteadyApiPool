// A new Azure AI Gateway (Microsoft.ApiManagement/aigateways) for the pool manager, when none is
// given: Standard, with a system-assigned identity, which calls the Foundry members. Billed per
// request.
targetScope = 'resourceGroup'

@minLength(3)
@maxLength(24)
param name string
param location string
param tags object = {}

#disable-next-line BCP081
resource gateway 'Microsoft.ApiManagement/aigateways@2026-11-01' = {
  name: name
  location: location
  tags: union(tags, { 'managed-by': 'steadyapipool' })
  sku: { name: 'Standard' }
  identity: { type: 'SystemAssigned' }
  properties: { publicNetworkAccess: 'Enabled' }
}

output name string = gateway.name
