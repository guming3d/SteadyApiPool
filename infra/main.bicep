// SteadyApiPool: one Linux App Service with Entra sign-in (Easy Auth) and a
// system-assigned managed identity that manages the pools of one gateway:
//
// - an API Management service (product 'apim'): give an existing one with apimName, or leave it
//   empty to create a new one in this resource group. An existing service is never changed here.
// - a LiteLLM gateway (product 'litellm'): deploy it first (the local app's setup page does it on
//   Azure Container Apps in one step, or on a new AKS cluster), then name it here. The gateway is
//   never changed here: its secrets are generated once when it is deployed, and a re-provision
//   must not rotate them.
targetScope = 'resourceGroup'

@minLength(1)
@maxLength(64)
@description('azd environment name; used for naming and tags.')
param environmentName string

param location string = resourceGroup().location

@allowed([ '', 'apim', 'litellm' ])
@description('The kind of gateway: apim or litellm. Empty decides from the names given: a LiteLLM gateway has gatewayName (or gatewayUrl), anything else is APIM.')
param product string = ''

// -- APIM ----------------------------------------------------------------------------------

@description('APIM: the service to manage. Empty creates a new one in this resource group.')
param apimName string = ''

@description('APIM: resource group of an existing service. Defaults to this resource group.')
param apimResourceGroup string = resourceGroup().name

@description('APIM: subscription of an existing service. Defaults to this subscription.')
param apimSubscriptionId string = subscription().subscriptionId

@description('APIM: tier of a new service (when apimName is empty). Developer takes 30-40 minutes to create and has no SLA.')
@allowed([ 'BasicV2', 'StandardV2', 'Developer' ])
param apimSkuName string = 'BasicV2'

@description('APIM: publisher email of a new service. Defaults to the first entry of owners, which must then be an email address.')
param apimPublisherEmail string = ''

@description('APIM: publisher name of a new service.')
param apimPublisherName string = 'APIM Backend Pool Manager'

@description('APIM: 2024-05-01 (GA) cannot read or write pool.sessionAffinity; the preview version can.')
param poolApiVersion string = '2025-09-01-preview'

// -- LiteLLM -------------------------------------------------------------------------------

@description('LiteLLM: the gateway\'s Container App name, or the display name of a gateway elsewhere (with gatewayUrl).')
param gatewayName string = ''

@description('LiteLLM: resource group of the gateway\'s Container App. Defaults to this resource group.')
param gatewayResourceGroup string = resourceGroup().name

@description('LiteLLM: subscription of the gateway\'s Container App. Defaults to this subscription.')
param gatewaySubscriptionId string = subscription().subscriptionId

@description('LiteLLM: a gateway that is not on Azure Container Apps (AKS, any LiteLLM proxy with a database): its HTTPS URL. Empty = the Container App gatewayName.')
param gatewayUrl string = ''

@secure()
@description('LiteLLM, with gatewayUrl: the gateway\'s master key. It is stored as an App Service setting.')
param gatewayMasterKey string = ''

@description('LiteLLM, with gatewayUrl: object ID of the managed identity the gateway calls Foundry as, so the setup page can check and grant its access.')
param gatewayPrincipalId string = ''

@description('LiteLLM, with gatewayUrl: resource ID of the Application Insights component its OpenTelemetry goes to, for the Monitor tab.')
param gatewayAppInsightsId string = ''

// -- both ----------------------------------------------------------------------------------

@description('Client ID of the Entra app registration used for App Service Authentication (see README).')
param entraClientId string

@description('Comma-separated UPNs/emails or object IDs allowed to change pools. Everyone else who can sign in is read-only.')
param owners string

@description('Subscriptions scanned for Azure OpenAI / Foundry deployments. Empty = the gateway\'s subscription.')
param discoverySubscriptionIds array = []

@description('Grant the app read-only access to Cognitive Services accounts and deployments in those subscriptions. Requires permission to create custom roles at subscription scope.')
param enableDeploymentDiscovery bool = true

@description('Easy Auth also gets an Azure Resource Manager token for the signed-in user, so the setup wizard can fix access with the user\'s own rights (grant the gateway\'s identity a role on Foundry accounts, enable APIM\'s identity). Needs the app registration steps in README "Delegated Azure access" first; without them sign-in fails.')
param enableDelegatedAzureAccess bool = false

@description('Linux App Service plan SKU. B1 is enough for an operator console.')
param appServiceSkuName string = 'B1'

var suffix = uniqueString(subscription().id, resourceGroup().id, environmentName)
var tags = { 'azd-env-name': environmentName }
var isApim = product == 'apim' || (empty(product) && empty(gatewayName) && empty(gatewayUrl))
var createApim = isApim && empty(apimName)
var external = !isApim && !empty(gatewayUrl)
var targetSubscription = isApim ? (createApim ? subscription().subscriptionId : apimSubscriptionId) : gatewaySubscriptionId
var targetGroup = isApim ? (createApim ? resourceGroup().name : apimResourceGroup) : gatewayResourceGroup
var discoverySubscriptions = empty(discoverySubscriptionIds) ? [ targetSubscription ] : discoverySubscriptionIds
// Each product keeps the role names and assignment seeds its deployments have always used, so a
// re-provision updates them in place.
var discoveryRole = isApim
  ? { name: 'APIM Backend Pool Deployment Reader', seed: 'apim-backend-pool-discovery' }
  : { name: 'AI Gateway Pool Deployment Reader', seed: 'ai-gateway-pool-discovery' }
// Easy Auth uses this identity as a federated credential of the app registration instead of a
// client secret. The system-assigned identity stays the one that calls ARM.
var authCredentialSetting = 'OVERRIDE_USE_MI_FIC_ASSERTION_CLIENTID'
var armUserScope = '${environment().resourceManager}user_impersonation'

resource authIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = if (enableDelegatedAzureAccess) {
  name: 'id-pool-auth-${suffix}'
  location: location
  tags: tags
}

module newApim 'modules/apim.bicep' = if (createApim) {
  name: 'apim-pool-manager-apim-${suffix}'
  params: {
    name: 'apim-pool-${suffix}'
    location: location
    tags: tags
    skuName: apimSkuName
    publisherEmail: empty(apimPublisherEmail) ? trim(first(split(owners, ','))) : apimPublisherEmail
    publisherName: apimPublisherName
  }
}

// Read from the module so the app and its role wait until a new service exists.
var managedApimName = createApim ? newApim!.outputs.name : apimName

resource plan 'Microsoft.Web/serverfarms@2024-11-01' = {
  name: 'plan-pool-${suffix}'
  location: location
  tags: tags
  kind: 'linux'
  sku: { name: appServiceSkuName }
  properties: { reserved: true }
}

resource site 'Microsoft.Web/sites@2024-11-01' = {
  name: 'app-pool-${suffix}'
  location: location
  tags: union(tags, { 'azd-service-name': 'web' })
  kind: 'app,linux'
  identity: enableDelegatedAzureAccess
    ? { type: 'SystemAssigned, UserAssigned', userAssignedIdentities: { '${authIdentity.id}': {} } }
    : { type: 'SystemAssigned' }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'PYTHON|3.12'
      appCommandLine: 'python -m uvicorn pool_manager.app:app --host 0.0.0.0 --port 8000 --proxy-headers'
      alwaysOn: true
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      http20Enabled: true
      healthCheckPath: '/health'
      appSettings: concat([
        { name: 'SCM_DO_BUILD_DURING_DEPLOYMENT', value: 'true' }
        { name: 'POOL_MANAGER_SUBSCRIPTION_ID', value: targetSubscription }
        { name: 'POOL_MANAGER_RESOURCE_GROUP', value: targetGroup }
        { name: 'POOL_MANAGER_AUTH_MODE', value: 'easy_auth' }
        { name: 'POOL_MANAGER_OWNERS', value: owners }
        { name: 'POOL_MANAGER_ARM_ENDPOINT', value: environment().resourceManager }
        { name: 'POOL_MANAGER_DISCOVERY_SUBSCRIPTIONS', value: join(discoverySubscriptions, ',') }
        { name: 'POOL_MANAGER_DELEGATED_ACCESS', value: string(enableDelegatedAzureAccess) }
      ], isApim ? [
        { name: 'POOL_MANAGER_APIM_NAME', value: managedApimName }
        { name: 'POOL_MANAGER_POOL_API_VERSION', value: poolApiVersion }
      ] : [
        { name: 'POOL_MANAGER_GATEWAY_NAME', value: gatewayName }
      ], external ? [
        { name: 'POOL_MANAGER_GATEWAY_URL', value: gatewayUrl }
        { name: 'POOL_MANAGER_GATEWAY_MASTER_KEY', value: gatewayMasterKey }
        { name: 'POOL_MANAGER_GATEWAY_PRINCIPAL_ID', value: gatewayPrincipalId }
        { name: 'POOL_MANAGER_GATEWAY_APP_INSIGHTS_ID', value: gatewayAppInsightsId }
      ] : [], enableDelegatedAzureAccess ? [
        { name: authCredentialSetting, value: authIdentity!.properties.clientId }
      ] : [])
    }
  }
}

// Deployments use Entra tokens (azd zip deploy); local FTP/SCM passwords are not needed.
resource ftpPolicy 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-11-01' = {
  parent: site
  name: 'ftp'
  properties: { allow: false }
}

resource scmPolicy 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-11-01' = {
  parent: site
  name: 'scm'
  properties: { allow: false }
}

// Easy Auth must stay enabled: the app trusts X-MS-CLIENT-PRINCIPAL-* only because App Service
// strips client-supplied copies of those headers when authentication is on.
resource auth 'Microsoft.Web/sites/config@2024-11-01' = {
  parent: site
  name: 'authsettingsV2'
  properties: {
    platform: { enabled: true }
    globalValidation: {
      requireAuthentication: true
      unauthenticatedClientAction: 'RedirectToLoginPage'
      redirectToProvider: 'azureactivedirectory'
      excludedPaths: [ '/health' ]
    }
    httpSettings: { requireHttps: true }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: union({
          clientId: entraClientId
          openIdIssuer: '${environment().authentication.loginEndpoint}${tenant().tenantId}/v2.0'
        }, enableDelegatedAzureAccess ? { clientSecretSettingName: authCredentialSetting } : {})
        // The ARM scope makes Easy Auth store an access token for the user (X-MS-TOKEN-AAD-*);
        // offline_access lets the frontend renew it through /.auth/refresh.
        login: enableDelegatedAzureAccess
          ? { loginParameters: [ 'scope=openid profile email offline_access ${armUserScope}' ] }
          : {}
        validation: { allowedAudiences: [ entraClientId, 'api://${entraClientId}' ] }
      }
    }
    login: { tokenStore: { enabled: true } }
  }
}

module apimAccess 'modules/apim-rbac.bicep' = if (isApim) {
  name: 'apim-pool-manager-rbac-${suffix}'
  scope: resourceGroup(targetSubscription, targetGroup)
  params: {
    apimName: managedApimName
    principalId: site.identity.principalId
  }
}

// A LiteLLM gateway elsewhere is reached with its master key; on Container Apps the app reads it.
module gatewayAccess 'modules/litellm-rbac.bicep' = if (!isApim && !external) {
  name: 'ai-gateway-pool-rbac-${suffix}'
  scope: resourceGroup(targetSubscription, targetGroup)
  params: {
    gatewayName: gatewayName
    principalId: site.identity.principalId
  }
}

module discoveryAccess 'modules/discovery-rbac.bicep' = [for subscriptionId in (enableDeploymentDiscovery ? discoverySubscriptions : []): {
  name: '${discoveryRole.seed}-${uniqueString(subscriptionId, suffix)}'
  scope: subscription(subscriptionId)
  params: {
    principalId: site.identity.principalId
    roleName: discoveryRole.name
    seed: discoveryRole.seed
  }
}]

output AZURE_LOCATION string = location
output WEB_APP_NAME string = site.name
output WEB_APP_URL string = 'https://${site.properties.defaultHostName}'
output WEB_APP_PRINCIPAL_ID string = site.identity.principalId
output GATEWAY_PRODUCT string = isApim ? 'apim' : 'litellm'
output APIM_SERVICE_NAME string = isApim ? managedApimName : ''
output GATEWAY_NAME string = isApim ? '' : gatewayName
output AUTH_REDIRECT_URI string = 'https://${site.properties.defaultHostName}/.auth/login/aad/callback'
// For the app registration's federated credential (README "Delegated Azure access").
output AUTH_FIC_ISSUER string = '${environment().authentication.loginEndpoint}${tenant().tenantId}/v2.0'
output AUTH_FIC_SUBJECT string = enableDelegatedAzureAccess ? authIdentity!.properties.principalId : ''
