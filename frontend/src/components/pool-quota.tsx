// Under a pool's members: what its Foundry deployments allow together, and a warning when that is
// less than the traffic the gateway was sized for. Shared by both products' pool editors.
import { CircleAlert, Gauge } from "lucide-react"

import { fill, useI18n } from "../i18n"
import { formatRate, quotaSummary, type SizedFor } from "../pool-quota"
import type { ModelCheck, Product } from "../types"

export function PoolQuotaNotice({ product, check, sized }: { product: Product; check: ModelCheck | undefined; sized: SizedFor | null }) {
  const { t, locale } = useI18n(product)
  const summary = quotaSummary(check, sized)
  if (!summary) return null
  const rate = (value: number) => formatRate(value, locale)
  return <>
    <div className="state">
      <Gauge size={14} />
      <span>
        {fill(t.quotaPool, { tpm: rate(summary.tpm), rpm: rate(summary.rpm) })}
        {summary.estimated && <small className="muted"> {t.quotaEstimated}</small>}
        {summary.unknown > 0 && <small className="muted"> {fill(t.quotaUnknown, { count: summary.unknown })}</small>}
      </span>
    </div>
    {summary.short && sized && <div className="state warning"><CircleAlert size={14} />{fill(t.quotaShort, { tpm: rate(sized.tpm), rpm: rate(sized.rpm) })}</div>}
  </>
}
