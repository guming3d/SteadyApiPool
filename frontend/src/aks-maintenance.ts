// The commands the gateway panel offers for maintaining an AKS gateway the app deployed. The
// cluster is the user's from then on: the app only manages pools, aliases and keys in LiteLLM.

// LiteLLM's Helm chart, as gateway_template.LITELLM_HELM_CHART installs it.
const LITELLM_HELM_CHART = "oci://ghcr.io/berriai/litellm-helm"

export type MaintenanceCommand = {
  key:
    | "maintainCredentials" | "maintainPods" | "maintainMasterKey" | "maintainUpgradeLitellm" | "maintainUpgradeAks"
    | "maintainScaleCheck" | "maintainScaleFixed" | "maintainScaleAuto"
  command: string
}

/** Subscription, resource group and name of a cluster's resource ID; null for anything else. */
export function parseClusterId(id: string): { subscription: string; group: string; name: string } | null {
  const match = /^\/subscriptions\/([^/]+)\/resourceGroups\/([^/]+)\/providers\/Microsoft\.ContainerService\/managedClusters\/([^/]+)$/i.exec(id)
  return match ? { subscription: match[1]!, group: match[2]!, name: match[3]! } : null
}

/** The commands for the gateway's cluster. Scaling depends on whether the system node pool
 * autoscales: litellm-aks.bicep deploys dev with one fixed node (no autoscaler) and production
 * with the autoscaler on (3-6 nodes), and Azure refuses --update-cluster-autoscaler on a pool
 * without it. The user may have changed that since, so both are offered, with a command to check
 * and the one the gateway's profile deployed first. */
export function maintenanceCommands(clusterId: string, profile: string | null = null): MaintenanceCommand[] {
  const cluster = parseClusterId(clusterId)
  if (!cluster) return []
  const target = `-g ${cluster.group} -n ${cluster.name} --subscription ${cluster.subscription}`
  const pool = `-g ${cluster.group} --cluster-name ${cluster.name} -n system --subscription ${cluster.subscription}`
  const fixed: MaintenanceCommand = {
    key: "maintainScaleFixed",
    command: `az aks nodepool scale ${pool} --node-count 2\naz aks nodepool update ${pool} --enable-cluster-autoscaler --min-count 1 --max-count 3`,
  }
  const auto: MaintenanceCommand = {
    key: "maintainScaleAuto",
    command: `az aks nodepool update ${pool} --update-cluster-autoscaler --min-count 3 --max-count 6\naz aks nodepool update ${pool} --disable-cluster-autoscaler`,
  }
  return [
    { key: "maintainCredentials", command: `az aks get-credentials ${target}` },
    { key: "maintainPods", command: "kubectl -n litellm get pods,gateway,certificate" },
    { key: "maintainMasterKey", command: "kubectl -n litellm get secret litellm-masterkey -o jsonpath='{.data.masterkey}' | base64 -d" },
    {
      key: "maintainUpgradeLitellm",
      command: `helm upgrade litellm ${LITELLM_HELM_CHART} -n litellm --reuse-values --version <new-version> --set image.tag=v<new-version>`,
    },
    { key: "maintainUpgradeAks", command: `az aks get-upgrades ${target} -o table\naz aks upgrade ${target} --kubernetes-version <version>` },
    {
      key: "maintainScaleCheck",
      command: `az aks nodepool show ${pool} --query "{nodes:count, autoscaler:enableAutoScaling, min:minCount, max:maxCount}" -o table`,
    },
    ...(profile === "production" ? [auto, fixed] : [fixed, auto]),
  ]
}

export function clusterPortalUrl(clusterId: string): string {
  return `https://portal.azure.com/#resource${clusterId}/overview`
}
