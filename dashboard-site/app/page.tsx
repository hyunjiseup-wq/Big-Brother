import type { Metadata } from "next";
import { Dashboard } from "./dashboard";
import { PrivateAccessGate } from "./private-access-gate";

export const metadata: Metadata = {
  title: "BB봇 운영 인사이트",
  description: "탐지 카드의 정탐·오탐과 서버 운영 지표를 한눈에 확인하는 관리자 대시보드",
};

type PageSearchParams = Record<string, string | string[] | undefined>;

export default async function Home({ searchParams }: { searchParams?: Promise<PageSearchParams> }) {
  const values = await searchParams;
  const initialSearch = new URLSearchParams();
  for (const [key, value] of Object.entries(values ?? {})) {
    if (typeof value === "string") initialSearch.set(key, value);
  }
  return <PrivateAccessGate><Dashboard initialSearch={initialSearch.toString()} /></PrivateAccessGate>;
}
