// The public app on Azure Container Apps. It has no Azure roles of its own beyond pulling its
// image: every Azure call runs as the signed-in user, through a token exchanged on their behalf
// (backend/pool_manager/entra.py). Its user-assigned identity is the app registration's
// federated credential, so no client secret exists anywhere.
targetScope = 'resourceGroup'

@minLength(1)
@maxLength(64)
@description('azd environment name')
param environmentName string

@description('Region for every resource')
param location string = resourceGroup().location

@description('Client ID of the multi-tenant app registration; the preprovision hook sets ENTRA_CLIENT_ID.')
param entraClientId string

@description('Comma-separated tenant IDs whose users may sign in. Empty = any work or school tenant.')
param allowedTenants string = ''

@description('Whether azd already deployed the app, so a re-provision keeps its current image.')
param webExists bool = false

@description('APIM service anyone may browse read-only as a guest, without signing in. Empty = no guest view.')
param guestApimName string = ''

@description('Resource group of the guest APIM service')
param guestApimResourceGroup string = ''

@description('Subscription of the guest APIM service; its Foundry deployments are shown too.')
param guestApimSubscriptionId string = subscription().subscriptionId

@minValue(1)
@maxValue(30)
param maxReplicas int = 3

var suffix = uniqueString(subscription().id, resourceGroup().id, environmentName)
var tags = { 'azd-env-name': environmentName }
var acrPullRole = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
var appName = 'ca-pool-public-${suffix}'
// Until azd deploys the real image; it answers on another port, so the first revision is unready.
var placeholderImage = 'mcr.microsoft.com/k8se/quickstart:latest'
var guestEnabled = !empty(guestApimName) && !empty(guestApimResourceGroup)

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-pool-public-${suffix}'
  location: location
  tags: tags
}

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-pool-public-${suffix}'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: 'crpoolpublic${suffix}'
  location: location
  tags: tags
  sku: { name: 'Basic' }
  properties: {
    adminUserEnabled: false
  }
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: registry
  name: guid(registry.id, identity.id, acrPullRole)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRole)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource containerEnv 'Microsoft.App/managedEnvironments@2025-01-01' = {
  name: 'cae-pool-public-${suffix}'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

// The guest view's only Azure role: Reader where the demo service and its Foundry accounts are.
module guestReader 'modules/guest-reader.bicep' = if (guestEnabled) {
  name: 'pool-public-guest-reader-${suffix}'
  scope: subscription(guestApimSubscriptionId)
  params: {
    principalId: identity.properties.principalId
  }
}

module current 'modules/current-image.bicep' = {
  name: 'pool-public-current-image-${suffix}'
  params: {
    exists: webExists
    name: appName
  }
}

resource app 'Microsoft.App/containerApps@2025-01-01' = {
  name: appName
  location: location
  tags: union(tags, { 'azd-service-name': 'web' })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    managedEnvironmentId: containerEnv.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
        // A load test runs on the replica that started it (benchmark.py, in the user's session):
        // the browser's polls and its stop have to reach that replica. Nothing else needs it.
        stickySessions: { affinity: 'sticky' }
      }
      registries: [
        {
          server: registry.properties.loginServer
          identity: identity.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'web'
          image: webExists ? current.outputs.image : placeholderImage
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            { name: 'POOL_MANAGER_AUTH_MODE', value: 'entra' }
            { name: 'POOL_MANAGER_ENTRA_CLIENT_ID', value: entraClientId }
            { name: 'POOL_MANAGER_FEDERATED_IDENTITY_CLIENT_ID', value: identity.properties.clientId }
            { name: 'POOL_MANAGER_ENTRA_AUTHORITY', value: environment().authentication.loginEndpoint }
            { name: 'POOL_MANAGER_ENTRA_ALLOWED_TENANTS', value: allowedTenants }
            { name: 'POOL_MANAGER_ARM_ENDPOINT', value: environment().resourceManager }
            { name: 'POOL_MANAGER_GUEST_SUBSCRIPTION_ID', value: guestEnabled ? guestApimSubscriptionId : '' }
            { name: 'POOL_MANAGER_GUEST_RESOURCE_GROUP', value: guestEnabled ? guestApimResourceGroup : '' }
            { name: 'POOL_MANAGER_GUEST_APIM_NAME', value: guestEnabled ? guestApimName : '' }
          ]
          probes: [
            {
              type: 'Liveness'
              httpGet: { path: '/health', port: 8000 }
              periodSeconds: 30
            }
            {
              type: 'Readiness'
              httpGet: { path: '/health', port: 8000 }
              periodSeconds: 10
            }
          ]
        }
      ]
      // Sessions are in memory and per replica: a request on another replica just exchanges
      // the user's token again (only a load test needs the same replica: sticky sessions).
      // One replica stays up so nobody waits for a cold start.
      scale: {
        minReplicas: 1
        maxReplicas: maxReplicas
      }
    }
  }
  dependsOn: [acrPull]
}

output AZURE_CONTAINER_REGISTRY_ENDPOINT string = registry.properties.loginServer
output PUBLIC_APP_URL string = 'https://${app.properties.configuration.ingress.fqdn}'
output PUBLIC_APP_IDENTITY_PRINCIPAL_ID string = identity.properties.principalId
output PUBLIC_APP_IDENTITY_TENANT_ID string = identity.properties.tenantId
output ENTRA_LOGIN_ENDPOINT string = environment().authentication.loginEndpoint
