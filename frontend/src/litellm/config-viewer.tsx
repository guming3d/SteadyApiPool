// Read-only views of a pool as LiteLLM config.yaml: what the gateway has now, and the diff a save
// would write. Both can be copied or exported, e.g. to keep the gateway's config in Git.
import { Copy, FileDown, LoaderCircle } from "lucide-react"
import { useMemo, useState } from "react"
import { useQuery } from "@tanstack/react-query"

import { queries } from "./api"
import { diffLines } from "./config-diff"
import { fill, useI18n } from "../i18n"
import { Button, Dialog, ErrorText } from "../components/ui"

export function ConfigActions({ yaml, filename }: { yaml: string; filename: string }) {
  const { t } = useI18n("litellm")
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    await navigator.clipboard.writeText(yaml)
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }
  const download = () => {
    const url = URL.createObjectURL(new Blob([yaml], { type: "application/yaml" }))
    const link = document.createElement("a")
    link.href = url
    link.download = filename
    link.click()
    URL.revokeObjectURL(url)
  }
  return <>
    <Button variant="ghost" onClick={copy}><Copy size={13} />{copied ? t.copied : t.copy}</Button>
    <Button onClick={download}><FileDown size={13} />{t.exportYaml}</Button>
  </>
}

export function ConfigCode({ yaml }: { yaml: string }) {
  return <pre className="policy-code">{yaml.split(/\r?\n/).map((line, index) =>
    <span key={index}>{line || " "}{"\n"}</span>)}</pre>
}

export function ConfigDiff({ before, after }: { before: string | null; after: string }) {
  const lines = useMemo(() => diffLines(before ?? "", after), [before, after])
  return <pre className="policy-code">{lines.map((line, index) =>
    <span key={index} data-diff={line.kind}>
      {line.kind === "added" ? "+ " : line.kind === "removed" ? "- " : "  "}{line.text || " "}{"\n"}
    </span>)}</pre>
}

export const configFilename = (poolId: string) => `${poolId}.litellm.yaml`

export function ConfigDialog({ poolId, onClose }: { poolId: string; onClose: () => void }) {
  const { t } = useI18n("litellm")
  const config = useQuery(queries.poolConfig(poolId))
  const yaml = config.data?.yaml ?? null
  return <Dialog
    open
    wide
    onClose={onClose}
    title={fill(t.configTitle, { pool: poolId })}
    description={t.configHint}
    footer={<>
      {yaml && <ConfigActions yaml={yaml} filename={configFilename(poolId)} />}
      <Button onClick={onClose}>{t.close}</Button>
    </>}
  >
    {config.isLoading ? <div className="state"><LoaderCircle className="spin" size={14} />{t.loading}</div>
      : config.error ? <ErrorText error={config.error} />
      : yaml ? <ConfigCode yaml={yaml} />
      : <div className="state">{t.configEmpty}</div>}
  </Dialog>
}
