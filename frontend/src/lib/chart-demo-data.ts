/** Shared demo datasets migrated from legacy chart-*.tsx demos */

export const MONTHLY_DESKTOP = [
  { month: "January", desktop: 186 },
  { month: "February", desktop: 305 },
  { month: "March", desktop: 237 },
  { month: "April", desktop: 73 },
  { month: "May", desktop: 209 },
  { month: "June", desktop: 214 },
] as const

export const MONTHLY_DESKTOP_MOBILE = [
  { month: "January", desktop: 186, mobile: 80 },
  { month: "February", desktop: 305, mobile: 200 },
  { month: "March", desktop: 237, mobile: 120 },
  { month: "April", desktop: 73, mobile: 190 },
  { month: "May", desktop: 209, mobile: 130 },
  { month: "June", desktop: 214, mobile: 140 },
] as const

export const MONTHLY_DESKTOP_MOBILE_OTHER = [
  { month: "January", desktop: 186, mobile: 80, other: 45 },
  { month: "February", desktop: 305, mobile: 200, other: 100 },
  { month: "March", desktop: 237, mobile: 120, other: 150 },
  { month: "April", desktop: 73, mobile: 190, other: 50 },
  { month: "May", desktop: 209, mobile: 130, other: 100 },
  { month: "June", desktop: 214, mobile: 140, other: 160 },
] as const

export const MONTHLY_VISITORS_SIGNED = [
  { month: "January", visitors: 186 },
  { month: "February", visitors: 205 },
  { month: "March", visitors: -207 },
  { month: "April", visitors: 173 },
  { month: "May", visitors: -209 },
  { month: "June", visitors: 214 },
] as const

export const BROWSER_SHARE = [
  { browser: "chrome", visitors: 275 },
  { browser: "safari", visitors: 200 },
  { browser: "firefox", visitors: 187 },
  { browser: "edge", visitors: 173 },
  { browser: "other", visitors: 90 },
] as const

export const LOAN_BY_STATE = [
  { STATE: "CA", TOTAL_PRINCIPAL: 1250000 },
  { STATE: "TX", TOTAL_PRINCIPAL: 980000 },
  { STATE: "NY", TOTAL_PRINCIPAL: 750000 },
  { STATE: "FL", TOTAL_PRINCIPAL: 620000 },
] as const

export const DELINQUENCY_TREND = [
  { quarter: "Q1 2024", delinquent: 42, current: 958 },
  { quarter: "Q2 2024", delinquent: 38, current: 962 },
  { quarter: "Q3 2024", delinquent: 45, current: 955 },
  { quarter: "Q4 2024", delinquent: 51, current: 949 },
] as const

export const SAMPLE_TABLE = {
  columns: ["LOAN_ID", "STATE", "PRIN_BAL", "STATUS"],
  rows: [
    ["L-1001", "CA", 250000, "IN_REPAY"],
    ["L-1002", "TX", 180000, "DELQ_1_9"],
    ["L-1003", "NY", 320000, "IN_REPAY"],
    ["L-1004", "FL", 95000, "PAID_CLOSED"],
  ],
} as const
