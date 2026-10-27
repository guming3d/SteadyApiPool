import assert from "node:assert/strict"
import test from "node:test"

import { clusterPortalUrl, maintenanceCommands, parseClusterId } from "../src/aks-maintenance.ts"

const CLUSTER = "/subscriptions/sub-1/resourceGroups/rg-ai/providers/Microsoft.ContainerService/managedClusters/gw-aks"

test("a cluster's resource ID names its subscription, group and cluster", () => {
  assert.deepEqual(parseClusterId(CLUSTER), { subscription: "sub-1", group: "rg-ai", name: "gw-aks" })
  assert.equal(parseClusterId("/subscriptions/s/resourceGroups/g/providers/Microsoft.App/containerApps/x"), null)
  assert.deepEqual(maintenanceCommands("nonsense"), [])
})

test("the maintenance commands target the gateway's cluster", () => {
  const commands = Object.fromEntries(maintenanceCommands(CLUSTER).map((item) => [item.key, item.command]))
  assert.equal(commands.maintainCredentials, "az aks get-credentials -g rg-ai -n gw-aks --subscription sub-1")
  assert.equal(
    commands.maintainScaleCheck,
    'az aks nodepool show -g rg-ai --cluster-name gw-aks -n system --subscription sub-1 --query "{nodes:count, autoscaler:enableAutoScaling, min:minCount, max:maxCount}" -o table',
  )
  assert.match(commands.maintainUpgradeLitellm, /helm upgrade litellm oci:\/\/ghcr\.io\/berriai\/litellm-helm -n litellm --reuse-values/)
  assert.match(commands.maintainMasterKey, /litellm-masterkey/)
  assert.equal(clusterPortalUrl(CLUSTER), `https://portal.azure.com/#resource${CLUSTER}/overview`)
})

test("scaling follows whether the node pool autoscales", () => {
  const pool = "-g rg-ai --cluster-name gw-aks -n system --subscription sub-1"
  // Dev deploys one fixed node: --update-cluster-autoscaler would be refused there.
  const dev = maintenanceCommands(CLUSTER, "dev")
  const fixed = dev.find((item) => item.key === "maintainScaleFixed").command.split("\n")
  assert.deepEqual(fixed, [
    `az aks nodepool scale ${pool} --node-count 2`,
    `az aks nodepool update ${pool} --enable-cluster-autoscaler --min-count 1 --max-count 3`,
  ])
  const auto = dev.find((item) => item.key === "maintainScaleAuto").command.split("\n")
  assert.deepEqual(auto, [
    `az aks nodepool update ${pool} --update-cluster-autoscaler --min-count 3 --max-count 6`,
    `az aks nodepool update ${pool} --disable-cluster-autoscaler`,
  ])
  const order = (profile) => maintenanceCommands(CLUSTER, profile).map((item) => item.key).filter((key) => key.startsWith("maintainScale"))
  assert.deepEqual(order("dev"), ["maintainScaleCheck", "maintainScaleFixed", "maintainScaleAuto"])
  assert.deepEqual(order(null), ["maintainScaleCheck", "maintainScaleFixed", "maintainScaleAuto"])
  assert.deepEqual(order("production"), ["maintainScaleCheck", "maintainScaleAuto", "maintainScaleFixed"])
})
