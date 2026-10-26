import { CircleHelp, X } from "lucide-react"
import { useEffect, useRef, type ButtonHTMLAttributes, type ReactNode, type Ref } from "react"

import { useI18n } from "../i18n"

type Variant = "primary" | "outline" | "ghost" | "destructive"

export function Button({
  variant = "outline",
  className = "",
  ref,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant; ref?: Ref<HTMLButtonElement> }) {
  return <button ref={ref} type="button" className={`btn btn-${variant} ${className}`} {...props} />
}

/** Native modal dialog: focus trap, Escape and the top layer come from the browser. */
export function Dialog({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  wide,
}: {
  open: boolean
  onClose: () => void
  title: ReactNode
  description?: ReactNode
  children: ReactNode
  footer?: ReactNode
  wide?: boolean
}) {
  const { t } = useI18n()
  const ref = useRef<HTMLDialogElement>(null)
  useEffect(() => {
    const dialog = ref.current
    if (!dialog) return
    if (open && !dialog.open) dialog.showModal()
    if (!open && dialog.open) dialog.close()
  }, [open])
  return <dialog
    ref={ref}
    className={`dialog ${wide ? "dialog-wide" : ""}`}
    onCancel={(event) => { event.preventDefault(); onClose() }}
  >
    {open && <>
      <header className="dialog-header">
        <div><h2>{title}</h2>{description && <p>{description}</p>}</div>
        <button type="button" className="icon-button" aria-label={t.close} onClick={onClose}><X size={16} /></button>
      </header>
      <div className="dialog-body">{children}</div>
      {footer && <footer className="dialog-footer">{footer}</footer>}
    </>}
  </dialog>
}

export function ConfirmDialog({
  open,
  title,
  description,
  confirmLabel,
  busy,
  error,
  onConfirm,
  onClose,
}: {
  open: boolean
  title: ReactNode
  description: ReactNode
  confirmLabel: string
  busy?: boolean
  error?: unknown
  onConfirm: () => void
  onClose: () => void
}) {
  const { t } = useI18n()
  return <Dialog
    open={open}
    onClose={() => { if (!busy) onClose() }}
    title={title}
    footer={<>
      <Button disabled={busy} onClick={onClose}>{t.cancel}</Button>
      <Button variant="destructive" disabled={busy} onClick={onConfirm}>{confirmLabel}</Button>
    </>}
  >
    <p className="muted">{description}</p>
    <ErrorText error={error} />
  </Dialog>
}

export function FieldHelp({ children }: { children: string }) {
  return <span className="field-help" title={children} aria-label={children} tabIndex={0}><CircleHelp size={13} /></span>
}

export function Field({ label, hint, children }: { label: ReactNode; hint?: ReactNode; children: ReactNode }) {
  return <label className="field"><span>{label}</span>{children}{hint && <small>{hint}</small>}</label>
}

export function ErrorText({ error }: { error: unknown }) {
  if (!error) return null
  return <div className="state error" role="alert">{error instanceof Error ? error.message : String(error)}</div>
}
