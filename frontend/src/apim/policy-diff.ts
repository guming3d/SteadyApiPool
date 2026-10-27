// Line diff for reviewing the API policy a bind would write. Policies are short, so a plain
// longest-common-subsequence table is fast enough; past the cap it degrades to replace-all.
export type DiffLine = { kind: "same" | "added" | "removed"; text: string }

const MAX_CELLS = 4_000_000

export function diffLines(before: string, after: string): DiffLine[] {
  const left = before ? before.split(/\r?\n/) : []
  const right = after.split(/\r?\n/)
  if (left.length * right.length > MAX_CELLS) {
    return [
      ...left.map((text): DiffLine => ({ kind: "removed", text })),
      ...right.map((text): DiffLine => ({ kind: "added", text })),
    ]
  }
  const width = right.length + 1
  const table = new Uint32Array((left.length + 1) * width)
  for (let i = left.length - 1; i >= 0; i--) {
    for (let j = right.length - 1; j >= 0; j--) {
      table[i * width + j] = left[i] === right[j]
        ? table[(i + 1) * width + j + 1] + 1
        : Math.max(table[(i + 1) * width + j], table[i * width + j + 1])
    }
  }
  const lines: DiffLine[] = []
  let i = 0
  let j = 0
  while (i < left.length && j < right.length) {
    if (left[i] === right[j]) {
      lines.push({ kind: "same", text: left[i] })
      i++
      j++
    } else if (table[(i + 1) * width + j] >= table[i * width + j + 1]) {
      lines.push({ kind: "removed", text: left[i++] })
    } else {
      lines.push({ kind: "added", text: right[j++] })
    }
  }
  while (i < left.length) lines.push({ kind: "removed", text: left[i++] })
  while (j < right.length) lines.push({ kind: "added", text: right[j++] })
  return lines
}

/** Lines inside this app's marker comments, so the viewer can highlight what it owns. */
export function managedLineFlags(xml: string): boolean[] {
  let inside = false
  return xml.split(/\r?\n/).map((line) => {
    if (line.includes("apim-backend-pool:") && line.includes(":begin")) inside = true
    const flag = inside
    if (line.includes("apim-backend-pool:") && line.includes(":end")) inside = false
    return flag
  })
}
