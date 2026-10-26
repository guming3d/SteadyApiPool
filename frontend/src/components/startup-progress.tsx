// Shown while the first setup report runs: it calls ARM step by step and can take half a
// minute, so say which step it is on rather than only "Loading".
import { CheckCircle2, LoaderCircle } from "lucide-react"
import { useEffect, useState } from "react"
import { useQuery } from "@tanstack/react-query"

import { queries } from "../api"
import { fill, useI18n } from "../i18n"
import { progressRows, SLOW_SECONDS, type ProgressRow } from "../setup-progress"

type Messages = ReturnType<typeof useI18n>["t"]

const LABEL: Record<ProgressRow["key"], keyof Messages> = {
  connecting: "startupConnecting",
  sign_in: "startupSignIn",
  gateway_target: "startupGatewayTarget",
  gateway_access: "startupGatewayAccess",
  app_gateway: "startupAppGateway",
  gateway_health: "startupGatewayHealth",
  discovery: "startupDiscovery",
  gateway_identity: "startupGatewayIdentity",
  members: "startupMembers",
  foundry_access: "startupFoundryAccess",
}

export function StartupProgress() {
  const { t } = useI18n()
  const progress = useQuery(queries.setupProgress())
  const [started] = useState(() => Date.now())
  const [seconds, setSeconds] = useState(0)
  useEffect(() => {
    const timer = setInterval(() => setSeconds(Math.floor((Date.now() - started) / 1000)), 1000)
    return () => clearInterval(timer)
  }, [started])
  const rows = progressRows(progress.data)

  return <div className="page"><section className="startup" aria-live="polite" aria-busy="true">
    <h2><LoaderCircle className="spin" size={16} />{t.startupTitle}</h2>
    <p className="muted">{t.startupHint}</p>
    <ol className="startup-steps">
      {rows.map((row, index) => <li key={`${row.key}-${index}`} data-state={row.running ? "running" : "done"}>
        {row.running ? <LoaderCircle className="spin" size={14} /> : <CheckCircle2 size={14} />}
        <span>{t[LABEL[row.key]]}</span>
        {row.subject && <code>{row.subject}</code>}
        {row.count && <small>{fill(t.startupCount, row.count)}</small>}
      </li>)}
    </ol>
    <p className="startup-elapsed">{fill(t.startupElapsed, { seconds })}</p>
    {seconds >= SLOW_SECONDS && <p className="muted">{t.startupSlow}</p>}
  </section></div>
}
