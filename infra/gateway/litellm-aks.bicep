// A LiteLLM AI gateway on Azure Kubernetes Service, deployed by the app's setup wizard with the
// signed-in user's own rights. This template creates the Azure side; the app then installs
// LiteLLM in the cluster through AKS Run Command (gateway_template.aks_bootstrap), and the
// cluster is the user's to maintain from there.
//
//   AKS cluster: Entra ID with Azure RBAC (no local accounts), OIDC issuer and workload
//                identity, the managed Gateway API with application routing's Istio
//                implementation (ingress), automatic patch and node image upgrades
//   User-assigned identity: what LiteLLM calls Foundry deployments as, federated with the
//                litellm/litellm service account; the setup wizard grants it Cognitive Services
//                OpenAI User on member accounts
//   Postgres Flexible Server: LiteLLM's model table, virtual keys and spend
//   Azure Managed Redis (production profile): cooldowns shared between replicas
//   Log Analytics + Application Insights: the gateway's OpenTelemetry spans
//
// Compiled to backend/pool_manager/templates/litellm-aks.json by
// scripts/build-gateway-template.py. The resources are the same as litellm-aca.bicep's where they
// overlap, and the template stays flat (no modules) so the deployment's operations name them.
targetScope = 'resourceGroup'

@minLength(2)
@maxLength(32)
@description('Name of the gateway: the cluster, the DNS label of its HTTPS address and the resource prefix.')
param name string

param location string = resourceGroup().location

@allowed([ 'dev', 'production' ])
@description('dev: one node, Free tier, no Redis. production: 3-6 nodes across zones, Standard tier, Redis.')
param profile string = 'dev'

@description('VM size of the cluster nodes.')
param nodeVmSize string = 'Standard_D4ds_v5'

@description('Availability zones the VM size has in this region (production only; empty for none).')
param zones array = []

@description('Object ID of the user deploying it: granted Azure Kubernetes Service RBAC Cluster Admin on the cluster.')
param deployerPrincipalId string

@secure()
@description('Password of the Postgres administrator (letters and digits).')
param postgresPassword string

param tags object = {}

var production = profile == 'production'
var suffix = uniqueString(resourceGroup().id, name)
var gatewayTags = union(tags, { 'managed-by': 'ai-gateway-pool', 'ai-gateway': name })
var postgresName = take('psql-${name}-${suffix}', 63)
var redisName = take('redis-${name}-${suffix}', 60)
// The namespace and service account LiteLLM runs as (gateway_template.AKS_NAMESPACE).
var serviceAccountSubject = 'system:serviceaccount:litellm:litellm'
var rbacClusterAdmin = 'b1ff04bb-8a4e-4dc4-8eb5-8693973ce19b'

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-${name}'
  location: location
  tags: gatewayTags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource insights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-${name}'
  location: location
  tags: gatewayTags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logs.id
  }
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${name}'
  location: location
  tags: gatewayTags
}

resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: postgresName
  location: location
  tags: gatewayTags
  sku: {
    name: production ? 'Standard_B2s' : 'Standard_B1ms'
    tier: 'Burstable'
  }
  properties: {
    version: '16'
    administratorLogin: 'litellm'
    administratorLoginPassword: postgresPassword
    storage: { storageSizeGB: 32 }
    backup: { backupRetentionDays: 7, geoRedundantBackup: 'Disabled' }
    highAvailability: { mode: 'Disabled' }
    network: { publicNetworkAccess: 'Enabled' }
    authConfig: { activeDirectoryAuth: 'Disabled', passwordAuth: 'Enabled' }
  }

  resource database 'databases' = {
    name: 'litellm'
    properties: { charset: 'UTF8', collation: 'en_US.utf8' }
  }

  // The cluster's outbound address is an Azure one.
  resource azureServices 'firewallRules' = {
    name: 'AllowAllAzureServicesAndResourcesWithinAzureIps'
    properties: { startIpAddress: '0.0.0.0', endIpAddress: '0.0.0.0' }
  }
}

resource redis 'Microsoft.Cache/redisEnterprise@2025-04-01' = if (production) {
  name: redisName
  location: location
  tags: gatewayTags
  sku: { name: 'Balanced_B0' }
  properties: { minimumTlsVersion: '1.2' }
}

resource redisDatabase 'Microsoft.Cache/redisEnterprise/databases@2025-04-01' = if (production) {
  parent: redis
  name: 'default'
  properties: {
    clientProtocol: 'Encrypted'
    port: 10000
    clusteringPolicy: 'EnterpriseCluster'
    evictionPolicy: 'NoEviction'
    accessKeysAuthentication: 'Enabled'
  }
}

// 2026-03-01 is the first version with the managed Gateway API and application routing's Istio
// implementation (the successor to the retired managed NGINX ingress).
#disable-next-line BCP081
resource cluster 'Microsoft.ContainerService/managedClusters@2026-03-01' = {
  name: name
  location: location
  tags: union(gatewayTags, {
    'ai-gateway-app-insights': insights.id
    'ai-gateway-profile': profile
  })
  sku: { name: 'Base', tier: production ? 'Standard' : 'Free' }
  identity: { type: 'SystemAssigned' }
  properties: {
    dnsPrefix: name
    aadProfile: { managed: true, enableAzureRBAC: true }
    disableLocalAccounts: true
    oidcIssuerProfile: { enabled: true }
    securityProfile: { workloadIdentity: { enabled: true } }
    agentPoolProfiles: [
      {
        name: 'system'
        mode: 'System'
        osType: 'Linux'
        osSKU: 'AzureLinux'
        vmSize: nodeVmSize
        count: production ? 3 : 1
        enableAutoScaling: production
        minCount: production ? 3 : null
        maxCount: production ? 6 : null
        availabilityZones: production ? zones : []
      }
    ]
    networkProfile: {
      networkPlugin: 'azure'
      networkPluginMode: 'overlay'
      loadBalancerSku: 'standard'
      outboundType: 'loadBalancer'
    }
    ingressProfile: {
      gatewayAPI: { installation: 'Standard' }
      webAppRouting: {
        gatewayAPIImplementations: { appRoutingIstio: { mode: 'Enabled' } }
      }
    }
    autoUpgradeProfile: {
      upgradeChannel: 'patch'
      nodeOSUpgradeChannel: 'NodeImage'
    }
  }
}

resource federation 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = {
  parent: identity
  name: 'litellm'
  properties: {
    issuer: cluster.properties.oidcIssuerProfile.issuerURL
    subject: serviceAccountSubject
    audiences: [ 'api://AzureADTokenExchange' ]
  }
}

// The deploying user can run kubectl and helm on the cluster (and the app, on their behalf,
// AKS Run Command): with Azure RBAC for Kubernetes, owning the resource isn't enough.
resource clusterAdmin 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(cluster.id, deployerPrincipalId, rbacClusterAdmin)
  scope: cluster
  properties: {
    principalId: deployerPrincipalId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', rbacClusterAdmin)
  }
}

output clusterId string = cluster.id
output fqdn string = '${name}.${location}.cloudapp.azure.com'
output identityClientId string = identity.properties.clientId
output principalId string = identity.properties.principalId
output appInsightsId string = insights.id
output postgresHost string = postgres.properties.fullyQualifiedDomainName
output redisHost string = production ? redis!.properties.hostName : ''
