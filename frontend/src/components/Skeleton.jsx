export function Skeleton({ className = '' }) {
  return <div className={`skeleton ${className}`} />
}

// A few stacked skeleton rows for list/table loading states.
export function SkeletonRows({ rows = 4, className = 'h-14' }) {
  return (
    <div className="flex flex-col gap-2.5">
      {Array.from({ length: rows }).map((_, i) => (
        <Skeleton key={i} className={`w-full rounded-xl ${className}`} />
      ))}
    </div>
  )
}
