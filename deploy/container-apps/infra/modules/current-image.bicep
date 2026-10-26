// The image the container app runs now, so provisioning again doesn't put the placeholder back
// over what azd deployed.
param exists bool
param name string

resource app 'Microsoft.App/containerApps@2025-01-01' existing = if (exists) {
  name: name
}

output image string = exists ? app!.properties.template.containers[0].image : ''
