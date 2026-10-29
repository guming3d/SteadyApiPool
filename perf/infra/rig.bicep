// The benchmark rig: a load VM and three stand-in Azure OpenAI deployments (mock-a, mock-b,
// mock-c, each its own host name like three Foundry accounts) with the control app that makes
// them fail on cue. Everything is tagged `perf-rig` so one resource group delete removes it.
// Deploy with deployMocks=false first, build the mock image into the registry, then again with
// deployMocks=true (see perf/README.md).

param location string = resourceGroup().location
param prefix string = 'perf'
@description('SSH public key for the load VM user `perf`.')
param sshPublicKey string
@description('The one address allowed to SSH to the VM (the operator\'s public IP).')
param allowedSshIp string
param vmSize string = 'Standard_D16ds_v5'
param deployMocks bool = false
param mockImage string = ''
// mock-a takes the whole load while it is healthy (the pools fail over by priority).
param primaryReplicas int = 8
param backupReplicas int = 4
param mockCpu string = '4.0'
param mockMemory string = '8Gi'
param mockWorkers int = 4
@secure()
@description('Token the control app asks for before it relays files between the operator and the VM.')
param relayToken string

var tags = { 'perf-rig': prefix }
var suffix = uniqueString(resourceGroup().id)

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${prefix}-logs-${suffix}'
  location: location
  tags: tags
  properties: { sku: { name: 'PerGB2018' }, retentionInDays: 30 }
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: '${prefix}acr${suffix}'
  location: location
  tags: tags
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false }
}

resource pullIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${prefix}-pull-${suffix}'
  location: location
  tags: tags
}

// AcrPull
resource pull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: registry
  name: guid(registry.id, pullIdentity.id, 'acrpull')
  properties: {
    principalId: pullIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  }
}

resource environment 'Microsoft.App/managedEnvironments@2025-01-01' = {
  name: '${prefix}-mocks-${suffix}'
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
    workloadProfiles: [{ name: 'Consumption', workloadProfileType: 'Consumption' }]
  }
}

var registries = [{ server: registry.properties.loginServer, identity: pullIdentity.id }]
var identity = { type: 'UserAssigned', userAssignedIdentities: { '${pullIdentity.id}': {} } }

resource control 'Microsoft.App/containerApps@2025-01-01' = if (deployMocks) {
  name: 'mock-control'
  location: location
  tags: tags
  identity: identity
  dependsOn: [pull]
  properties: {
    environmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      ingress: { external: true, targetPort: 8080, transport: 'http' }
      registries: registries
      secrets: [{ name: 'relay-token', value: relayToken }]
    }
    template: {
      containers: [
        {
          name: 'control'
          image: mockImage
          resources: { cpu: json('1.0'), memory: '2Gi' }
          env: [
            { name: 'MODE', value: 'control' }
            { name: 'WORKERS', value: '1' }
            { name: 'RELAY_TOKEN', secretRef: 'relay-token' }
          ]
        }
      ]
      scale: { minReplicas: 1, maxReplicas: 1 }
    }
  }
}

var mocks = [
  { name: 'mock-a', replicas: primaryReplicas }
  { name: 'mock-b', replicas: backupReplicas }
  { name: 'mock-c', replicas: backupReplicas }
]

resource mock 'Microsoft.App/containerApps@2025-01-01' = [for m in mocks: if (deployMocks) {
  name: m.name
  location: location
  tags: tags
  identity: identity
  dependsOn: [pull]
  properties: {
    environmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      ingress: { external: true, targetPort: 8080, transport: 'http' }
      registries: registries
    }
    template: {
      containers: [
        {
          name: 'mock'
          image: mockImage
          resources: { cpu: json(mockCpu), memory: mockMemory }
          env: [
            { name: 'REGION', value: m.name }
            { name: 'CONTROL_URL', value: 'https://mock-control.${environment.properties.defaultDomain}' }
            { name: 'WORKERS', value: string(mockWorkers) }
          ]
          probes: [{ type: 'Readiness', httpGet: { path: '/healthz', port: 8080 }, periodSeconds: 10 }]
        }
      ]
      scale: { minReplicas: m.replicas, maxReplicas: m.replicas }
    }
  }
}]

resource nsg 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: '${prefix}-vm-nsg'
  location: location
  tags: tags
  properties: {
    securityRules: [
      {
        name: 'ssh-from-operator'
        properties: {
          priority: 100
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: allowedSshIp
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '22'
        }
      }
    ]
  }
}

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: '${prefix}-vnet'
  location: location
  tags: tags
  properties: {
    addressSpace: { addressPrefixes: ['10.60.0.0/16'] }
    subnets: [{ name: 'vm', properties: { addressPrefix: '10.60.1.0/24', networkSecurityGroup: { id: nsg.id } } }]
  }
}

// An instance public IP: the VM's outbound connections use all of its ports, no SNAT pool.
resource ip 'Microsoft.Network/publicIPAddresses@2024-05-01' = {
  name: '${prefix}-vm-ip'
  location: location
  tags: tags
  sku: { name: 'Standard' }
  properties: { publicIPAllocationMethod: 'Static' }
}

resource nic 'Microsoft.Network/networkInterfaces@2024-05-01' = {
  name: '${prefix}-vm-nic'
  location: location
  tags: tags
  properties: {
    enableAcceleratedNetworking: true
    ipConfigurations: [
      {
        name: 'ip'
        properties: {
          subnet: { id: vnet.properties.subnets[0].id }
          privateIPAllocationMethod: 'Dynamic'
          publicIPAddress: { id: ip.id }
        }
      }
    ]
  }
}

var cloudInit = '''#cloud-config
package_update: true
packages: [python3-venv, python3-pip, sysstat, jq]
write_files:
  - path: /etc/sysctl.d/90-perf.conf
    content: |
      net.ipv4.ip_local_port_range = 1024 65535
      net.core.somaxconn = 65535
      net.ipv4.tcp_tw_reuse = 1
      net.ipv4.tcp_fin_timeout = 15
      fs.file-max = 2097152
  - path: /etc/security/limits.d/90-perf.conf
    content: |
      * soft nofile 1048576
      * hard nofile 1048576
runcmd:
  - sysctl --system
'''

resource vm 'Microsoft.Compute/virtualMachines@2024-07-01' = {
  name: '${prefix}-loadvm'
  location: location
  tags: tags
  identity: { type: 'SystemAssigned' }
  properties: {
    hardwareProfile: { vmSize: vmSize }
    osProfile: {
      computerName: '${prefix}-loadvm'
      adminUsername: 'perf'
      customData: base64(cloudInit)
      linuxConfiguration: {
        disablePasswordAuthentication: true
        ssh: { publicKeys: [{ path: '/home/perf/.ssh/authorized_keys', keyData: sshPublicKey }] }
      }
    }
    storageProfile: {
      imageReference: { publisher: 'Canonical', offer: 'ubuntu-24_04-lts', sku: 'server', version: 'latest' }
      osDisk: { createOption: 'FromImage', diskSizeGB: 64, managedDisk: { storageAccountType: 'Premium_LRS' } }
    }
    networkProfile: { networkInterfaces: [{ id: nic.id }] }
  }
}

output registry string = registry.properties.loginServer
output registryName string = registry.name
output vmIp string = ip.properties.ipAddress
output vmPrincipalId string = vm.identity.principalId
output mockDomain string = environment.properties.defaultDomain
output controlUrl string = 'https://mock-control.${environment.properties.defaultDomain}'
