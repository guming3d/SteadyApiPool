import assert from "node:assert/strict"
import test from "node:test"

import { containerAppPortalUrl, masterKeyCommand, parseContainerAppId } from "../../src/litellm/aca-maintenance.ts"

const APP = "/subscriptions/sub-1/resourceGroups/rg-ai/providers/Microsoft.App/containerApps/gw-aca"

test("a Container App's resource ID names its subscription, group and app", () => {
  assert.deepEqual(parseContainerAppId(APP), { subscription: "sub-1", group: "rg-ai", name: "gw-aca" })
  assert.equal(parseContainerAppId("/subscriptions/s/resourceGroups/g/providers/Microsoft.ContainerService/managedClusters/x"), null)
  assert.equal(masterKeyCommand("nonsense"), null)
  assert.equal(masterKeyCommand(""), null)
})

test("the master key command reads the gateway's own secret", () => {
  assert.equal(
    masterKeyCommand(APP),
    "az containerapp secret show -g rg-ai -n gw-aca --subscription sub-1 --secret-name litellm-master-key --query value -o tsv",
  )
  assert.equal(containerAppPortalUrl(APP), `https://portal.azure.com/#resource${APP}/overview`)
})
