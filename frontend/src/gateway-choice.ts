// Container Apps or AKS for the LiteLLM gateway, from a few answers. Pure, so node --test can
// import it; components/gateway-picker.tsx asks the questions.
//
// Container Apps is the default: the app deploys it in one step and there is no cluster to run.
// AKS is worth its upkeep when a team already runs Kubernetes, when self-hosted (GPU) models
// should sit next to the gateway, or for traffic past what a handful of replicas comfortably
// serves. Private networking works on both but isn't automated here.
import type { GatewayProfile } from "./types"

export type Platform = "aca" | "aks"
export type Stage = "trial" | "production"

export type ChoiceAnswers = {
  stage: Stage
  /** Peak tokens per minute, in millions. */
  peakTpmMillions: number
  /** Average input + output tokens per request. */
  tokensPerRequest: number
  /** An AKS cluster and people who run it already exist. */
  kubernetesTeam: boolean
  /** Models hosted on own GPUs should be served next to the gateway. */
  selfHostedModels: boolean
  /** The gateway must not be reachable from the internet. */
  privateNetwork: boolean
}

export type Reason =
  | "choiceReasonSimple" | "choiceReasonTeam" | "choiceReasonGpu" | "choiceReasonScale"
  | "choiceReasonPrivateAks" | "choiceReasonPrivateManual" | "choiceReasonProduction"
  | "choiceReasonTrial"

export type Recommendation = {
  platform: Platform
  profile: GatewayProfile
  /** Peak requests per second the answers imply. */
  rps: number
  /** Replicas to plan for at the peak (a dev gateway runs one). */
  replicas: number
  reasons: Reason[]
}

export const DEFAULT_ANSWERS: ChoiceAnswers = {
  stage: "trial",
  peakTpmMillions: 1,
  tokensPerRequest: 10_000,
  kubernetesTeam: false,
  selfHostedModels: false,
  privateNetwork: false,
}

/** Conservative requests per second one gateway replica serves (LiteLLM, one worker, 2 vCPU). */
export const RPS_PER_REPLICA = 50
/** The production profile's autoscale limit (maxReplicas in infra/gateway/litellm-aca.bicep). */
export const ACA_MAX_REPLICAS = 5
/** Past what the Container Apps profile can run, plan the gateway as a platform on AKS (node
 *  pools, autoscaling, load tests). */
export const AKS_RPS = ACA_MAX_REPLICAS * RPS_PER_REPLICA

export function peakRps(answers: Pick<ChoiceAnswers, "peakTpmMillions" | "tokensPerRequest">) {
  const tokens = Math.max(1, answers.tokensPerRequest)
  return Math.max(0, answers.peakTpmMillions) * 1_000_000 / 60 / tokens
}

export function recommend(answers: ChoiceAnswers): Recommendation {
  const rps = peakRps(answers)
  const reasons: Reason[] = []
  let platform: Platform = "aca"
  if (answers.selfHostedModels) {
    platform = "aks"
    reasons.push("choiceReasonGpu")
  }
  if (answers.kubernetesTeam) {
    platform = "aks"
    reasons.push("choiceReasonTeam")
  }
  if (rps > AKS_RPS) {
    platform = "aks"
    reasons.push("choiceReasonScale")
  }
  if (answers.privateNetwork) {
    // Both can be private; with nobody to run a cluster, Container Apps in a VNet is less work.
    reasons.push(platform === "aks" ? "choiceReasonPrivateAks" : "choiceReasonPrivateManual")
  }
  if (platform === "aca") reasons.unshift("choiceReasonSimple")
  const production = answers.stage === "production"
  reasons.push(production ? "choiceReasonProduction" : "choiceReasonTrial")
  return {
    platform,
    profile: production ? "production" : "dev",
    rps,
    replicas: production ? Math.min(30, Math.max(2, Math.ceil(rps / RPS_PER_REPLICA))) : 1,
    reasons,
  }
}

/** List price per month (USD, East US 2, retail), quiet to busy, of what each profile deploys:
 *  Container Apps replicas, Postgres and, for production, Redis. The gateway panel shows the
 *  real numbers for the region once it exists. */
export const PROFILE_ESTIMATE: Record<GatewayProfile, { low: number; high: number }> = {
  dev: { low: 46, high: 115 },
  production: { low: 171, high: 865 },
}

/** The same for a new AKS cluster (US list prices): Standard_D4ds_v5 nodes ($0.24 an hour,
 *  1 for dev, 3-6 for production), the Standard tier's uptime SLA for production, PostgreSQL,
 *  Redis, and the load balancer. The gateway panel shows the chosen region's price. */
export const AKS_PROFILE_ESTIMATE: Record<GatewayProfile, { low: number; high: number }> = {
  dev: { low: 190, high: 230 },
  production: { low: 670, high: 1200 },
}

// Mirrors GATEWAY_NAME_PATTERN in pool_manager/models.py: lower case, a letter first, no
// trailing or double hyphen, 2-32 characters.
export const GATEWAY_NAME = /^[a-z](?:[a-z0-9]|-[a-z0-9]){1,30}$/

export const isGatewayName = (name: string) => name.length <= 32 && GATEWAY_NAME.test(name)

export function suggestGatewayName(random: () => number = Math.random) {
  return `ai-gateway-${random().toString(36).slice(2, 7)}`
}
