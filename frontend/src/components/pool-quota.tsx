// Under a pool's members: what its Foundry deployments allow together, what the pool takes as its
// members are set (failover, balanced or weighted) before the first one is rate-limited, and a
// warning when that is less than the traffic the gateway was sized for. Shared by both products'
// pool editors.
import { CircleAlert, Gauge } from "lucide-react"

import { fill, useI18n } from "../i18n"
import { formatRate, quotaSummary, splitLimit, type SizedFor } from "../pool-quota"
import type { MemberModel, ModelCheck, PoolMember, Product } from "../types"

export function PoolQuotaNotice({ product, check, members, sized }: {
  product: Product
  check: ModelCheck | undefined
  /** The draft's members, whose priorities and weights split the traffic. */
  members: PoolMember[]
  sized: SizedFor | null
}) {
  const { t, locale } = useI18n(product)
  const summary = quotaSummary(check, sized)
  if (!summary) return null
  const rate = (value: number) => formatRate(value, locale)
  const split = splitLimit(check, members)
  const known = split && split.tpm !== null && split.rpm !== null ? { tpm: rate(split.tpm), rpm: rate(split.rpm) } : null
  return <>
    <div className="state">
      <Gauge size={14} />
      <span>
        {fill(t.quotaPool, { tpm: rate(summary.tpm), rpm: rate(summary.rpm) })}
        {summary.estimated && <small className="muted"> {t.quotaEstimated}</small>}
        {summary.unknown > 0 && <small className="muted"> {fill(t.quotaUnknown, { count: summary.unknown })}</small>}
      </span>
    </div>
    {known && split && members.length > 1 && <div className="state">
      <Gauge size={14} />
      <span>{fill(split.active === 1 ? t.quotaFailover : t.quotaSplit, known)}</span>
    </div>}
    {summary.short && sized && <div className="state warning"><CircleAlert size={14} />{fill(t.quotaShort, { tpm: rate(sized.tpm), rpm: rate(sized.rpm) })}</div>}
  </>
}

/** On a member's row: its deployment type and per-minute limits, to choose the primary or weights by. */
export function MemberQuota({ product, model }: { product: Product; model: MemberModel | undefined }) {
  const { t, locale } = useI18n(product)
  // Custom endpoints have no deployment to read limits from.
  if (!model?.deployment_id) return null
  const type = model.deployment_type ? `${model.deployment_type} · ` : ""
  if (model.tpm === null && model.rpm === null) return <span className="muted">{type}{t.quotaMemberUnknown}</span>
  const rate = (value: number | null) => value === null ? "—" : formatRate(value, locale)
  return <span className="muted" title={model.quota_source === "capacity" ? t.quotaEstimated : undefined}>
    {type}{fill(t.quotaMemberLimits, { tpm: rate(model.tpm), rpm: rate(model.rpm) })}
    {model.quota_source === "capacity" && ` ${t.quotaMemberEstimated}`}
  </span>
}
