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
  assert.match(commands.maintainScale, /--cluster-name gw-aks -n system/)
  assert.match(commands.maintainUpgradeLitellm, /helm upgrade litellm oci:\/\/ghcr\.io\/berriai\/litellm-helm -n litellm --reuse-values/)
  assert.match(commands.maintainMasterKey, /litellm-masterkey/)
  assert.equal(clusterPortalUrl(CLUSTER), `https://portal.azure.com/#resource${CLUSTER}/overview`)
})
