import type { ReactNode } from "react"

export function ChartSection({
  title,
  children,
}: {
  title: string
  children: ReactNode
}) {
  return (
    <section className="flex flex-col gap-4">
      <h2 className="text-lg font-semibold tracking-tight">{title}</h2>
      <div className="grid gap-6 md:grid-cols-2">{children}</div>
    </section>
  )
}

export function ChartGridItem({
  fullWidth = false,
  children,
}: {
  fullWidth?: boolean
  children: ReactNode
}) {
  return (
    <div className={fullWidth ? "md:col-span-2" : undefined}>{children}</div>
  )
}
