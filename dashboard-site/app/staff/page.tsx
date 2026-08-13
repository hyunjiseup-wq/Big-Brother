import type { Metadata } from "next";
import { StaffLedger } from "./staff-ledger";

export const metadata: Metadata = {
  title: "스태프 제재 인수인계 | BB봇",
  description: "관리자 전용 경고·타임아웃 적용 및 해제 기록",
  robots: { index: false, follow: false },
};

export default function StaffPage() {
  return <StaffLedger />;
}
