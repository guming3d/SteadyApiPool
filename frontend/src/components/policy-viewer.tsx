// Read-only views of an API policy: the current XML with this app's blocks highlighted, and
// the diff a bind would write. Both can be copied or exported for a manual review.
import { Copy, FileDown, LoaderCircle } from "lucide-react"
import { useMemo, useState } from "react"
import { useQuery } from "@tanstack/react-query"

import { queries } from "../api"
import { fill, useI18n } from "../i18n"
import { diffLines, managedLineFlags } from "../policy-diff"
import type { ApiView } from "../types"
import { Button, Dialog, ErrorText } from "./ui"

export function PolicyActions({ xml, filename }: { xml: string; filename: string }) {
  const { t } = useI18n()
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    await navigator.clipboard.writeText(xml)
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }
  const download = () => {
    const url = URL.createObjectURL(new Blob([xml], { type: "application/xml" }))
    const link = document.createElement("a")
    link.href = url
    link.download = filename
    link.click()
    URL.revokeObjectURL(url)
  }
  return <>
    <Button variant="ghost" onClick={copy}><Copy size={13} />{copied ? t.copied : t.copy}</Button>
    <Button onClick={download}><FileDown size={13} />{t.exportXml}</Button>
  </>
}

export function PolicyCode({ xml }: { xml: string }) {
  const flags = useMemo(() => managedLineFlags(xml), [xml])
  return <pre className="policy-code">{xml.split(/\r?\n/).map((line, index) =>
    <span key={index} data-managed={flags[index]}>{line || " "}{"\n"}</span>)}</pre>
}

export function PolicyDiff({ before, after }: { before: string | null; after: string }) {
  const lines = useMemo(() => diffLines(before ?? "", after), [before, after])
  return <pre className="policy-code">{lines.map((line, index) =>
    <span key={index} data-diff={line.kind}>
      {line.kind === "added" ? "+ " : line.kind === "removed" ? "- " : "  "}{line.text || " "}{"\n"}
    </span>)}</pre>
}

export const policyFilename = (apiId: string) => `${apiId}-policy.xml`

export function PolicyDialog({ api, onClose }: { api: ApiView; onClose: () => void }) {
  const { t } = useI18n()
  const policy = useQuery(queries.policy(api.id))
  const xml = policy.data?.xml ?? null
  return <Dialog
    open
    wide
    onClose={onClose}
    title={fill(t.policyTitle, { api: api.display_name })}
    description={t.policyManagedHint}
    footer={<>
      {xml && <PolicyActions xml={xml} filename={policyFilename(api.id)} />}
      <Button onClick={onClose}>{t.close}</Button>
    </>}
  >
    {policy.isLoading ? <div className="state"><LoaderCircle className="spin" size={14} />{t.loading}</div>
      : policy.error ? <ErrorText error={policy.error} />
      : xml ? <PolicyCode xml={xml} />
      : <div className="state">{t.noApiPolicy}</div>}
  </Dialog>
}
