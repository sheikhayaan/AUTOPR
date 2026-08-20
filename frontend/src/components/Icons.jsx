// Hand-rolled inline SVG icons (stroke = currentColor). No icon dependency —
// keeps the bundle lean and the visual language consistent.
function Svg({ children, className = 'h-5 w-5', ...props }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
      {...props}
    >
      {children}
    </svg>
  )
}

export function LogoMark({ className = 'h-8 w-8' }) {
  return (
    <svg viewBox="0 0 32 32" className={className} aria-hidden="true">
      <rect width="32" height="32" rx="9" fill="url(#lg)" />
      <path
        d="M9 21V11l7 5 7-5v10"
        stroke="white"
        strokeWidth="2.4"
        fill="none"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <defs>
        <linearGradient id="lg" x1="0" y1="0" x2="32" y2="32" gradientUnits="userSpaceOnUse">
          <stop stopColor="#818cf8" />
          <stop offset="1" stopColor="#4f46e5" />
        </linearGradient>
      </defs>
    </svg>
  )
}

export const DashboardIcon = (p) => (
  <Svg {...p}>
    <rect x="3" y="3" width="7.5" height="9" rx="1.5" />
    <rect x="13.5" y="3" width="7.5" height="5.5" rx="1.5" />
    <rect x="13.5" y="12" width="7.5" height="9" rx="1.5" />
    <rect x="3" y="15.5" width="7.5" height="5.5" rx="1.5" />
  </Svg>
)

export const InboxIcon = (p) => (
  <Svg {...p}>
    <path d="M3 13l2.5-8h13L21 13" />
    <path d="M3 13v6a1 1 0 001 1h16a1 1 0 001-1v-6" />
    <path d="M3 13h5l1.5 2.5h5L16 13h5" />
  </Svg>
)

export const ActivityIcon = (p) => (
  <Svg {...p}>
    <path d="M3 12h4l2.5 7 5-15L17 12h4" />
  </Svg>
)

export const CheckIcon = (p) => (
  <Svg {...p}>
    <path d="M20 6L9 17l-5-5" />
  </Svg>
)

export const CheckCircleIcon = (p) => (
  <Svg {...p}>
    <circle cx="12" cy="12" r="9" />
    <path d="M8.5 12.5l2.5 2.5 4.5-5" />
  </Svg>
)

export const XIcon = (p) => (
  <Svg {...p}>
    <path d="M18 6L6 18M6 6l12 12" />
  </Svg>
)

export const RefreshIcon = (p) => (
  <Svg {...p}>
    <path d="M21 12a9 9 0 11-2.64-6.36M21 4v5h-5" />
  </Svg>
)

export const ChevronDownIcon = (p) => (
  <Svg {...p}>
    <path d="M6 9l6 6 6-6" />
  </Svg>
)

export const CommentIcon = (p) => (
  <Svg {...p}>
    <path d="M21 11.5a8.5 8.5 0 01-12.2 7.6L3 21l1.9-5.8A8.5 8.5 0 1121 11.5z" />
  </Svg>
)

export const AlertIcon = (p) => (
  <Svg {...p}>
    <path d="M10.3 3.7L1.8 18a2 2 0 001.7 3h17a2 2 0 001.7-3L13.7 3.7a2 2 0 00-3.4 0z" />
    <path d="M12 9v4M12 17h.01" />
  </Svg>
)

export const InfoIcon = (p) => (
  <Svg {...p}>
    <circle cx="12" cy="12" r="9" />
    <path d="M12 11v5M12 8h.01" />
  </Svg>
)

export const ClockIcon = (p) => (
  <Svg {...p}>
    <circle cx="12" cy="12" r="9" />
    <path d="M12 7v5l3.5 2" />
  </Svg>
)

export const PullRequestIcon = (p) => (
  <Svg {...p}>
    <circle cx="6" cy="6" r="2.5" />
    <circle cx="6" cy="18" r="2.5" />
    <circle cx="18" cy="18" r="2.5" />
    <path d="M6 8.5v7M18 15.5V12a4 4 0 00-4-4h-3m0 0l2.5-2.5M11 8l2.5 2.5" />
  </Svg>
)

export const ShieldIcon = (p) => (
  <Svg {...p}>
    <path d="M12 3l7 3v5c0 4.5-3 8-7 10-4-2-7-5.5-7-10V6l7-3z" />
    <path d="M9 12l2 2 4-4" />
  </Svg>
)

export const BoltIcon = (p) => (
  <Svg {...p}>
    <path d="M13 2L4.5 13.5H11L10 22l8.5-11.5H12L13 2z" />
  </Svg>
)

export const ExternalLinkIcon = (p) => (
  <Svg {...p}>
    <path d="M15 3h6v6M21 3l-9 9M18 14v5a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h5" />
  </Svg>
)

export const SearchIcon = (p) => (
  <Svg {...p}>
    <circle cx="11" cy="11" r="7" />
    <path d="M21 21l-4.3-4.3" />
  </Svg>
)

export const SparkleIcon = (p) => (
  <Svg {...p}>
    <path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8L12 3z" />
  </Svg>
)
