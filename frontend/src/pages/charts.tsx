import { ChartGallery } from "@/components/report/chart-gallery"

export default function ChartsPage() {
  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-y-auto p-4 md:gap-12 md:p-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Chart Gallery</h1>
      </header>
      <ChartGallery />
    </div>
  )
}
