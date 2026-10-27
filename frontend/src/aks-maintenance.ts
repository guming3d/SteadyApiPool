// The commands the gateway panel offers for maintaining an AKS gateway the app deployed. The
// cluster is the user's from then on: the app only manages pools, aliases and keys in LiteLLM.

// LiteLLM's Helm chart, as gateway_template.LITELLM_HELM_CHART installs it.
const LITELLM_HELM_CHART = "oci://ghcr.io/berriai/litellm-helm"

export type MaintenanceCommand = {
  key: "maintainCredentials" | "maintainPods" | "maintainMasterKey" | "maintainUpgradeLitellm" | "maintainUpgradeAks" | "maintainScale"
  command: string
}

/** Subscription, resource group and name of a cluster's resource ID; null for anything else. */
export function parseClusterId(id: string): { subscription: string; group: string; name: string } | null {
  const match = /^\/subscriptions\/([^/]+)\/resourceGroups\/([^/]+)\/providers\/Microsoft\.ContainerService\/managedClusters\/([^/]+)$/i.exec(id)
  return match ? { subscription: match[1]!, group: match[2]!, name: match[3]! } : null
}

export function maintenanceCommands(clusterId: string): MaintenanceCommand[] {
  const cluster = parseClusterId(clusterId)
  if (!cluster) return []
  const target = `-g ${cluster.group} -n ${cluster.name} --subscription ${cluster.subscription}`
  return [
    { key: "maintainCredentials", command: `az aks get-credentials ${target}` },
    { key: "maintainPods", command: "kubectl -n litellm get pods,gateway,certificate" },
    { key: "maintainMasterKey", command: "kubectl -n litellm get secret litellm-masterkey -o jsonpath='{.data.masterkey}' | base64 -d" },
    {
      key: "maintainUpgradeLitellm",
      command: `helm upgrade litellm ${LITELLM_HELM_CHART} -n litellm --reuse-values --version <new-version> --set image.tag=v<new-version>`,
    },
    { key: "maintainUpgradeAks", command: `az aks get-upgrades ${target} -o table\naz aks upgrade ${target} --kubernetes-version <version>` },
    { key: "maintainScale", command: `az aks nodepool update ${target.replace(`-n ${cluster.name}`, `--cluster-name ${cluster.name} -n system`)} --update-cluster-autoscaler --min-count 3 --max-count 6` },
  ]
}

export function clusterPortalUrl(clusterId: string): string {
  return `https://portal.azure.com/#resource${clusterId}/overview`
}
