import type { Metadata } from "next";
import { Dashboard } from "./dashboard";

export const metadata: Metadata = {
  title: "BB봇 운영 인사이트",
  description: "탐지 카드의 정탐·오탐과 서버 운영 지표를 한눈에 확인하는 관리자 대시보드",
};

export default function Home() {
  return <Dashboard />;
}
