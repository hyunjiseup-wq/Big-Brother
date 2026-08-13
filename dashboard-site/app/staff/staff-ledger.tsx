"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import Link from "next/link";

type SanctionRecord = {
  event_id: string;
  user_id: string;
  user_display: string;
  action_type: "WARNING" | "DELETE" | "TIMEOUT" | "KICK" | "BAN";
  reason: string;
  source: string;
  status: "active" | "released" | "expired";
  issued_at: string;
  expires_at: string | null;
  released_at: string | null;
  issued_by_display: string | null;
  released_by_display: string | null;
  release_reason: string | null;
};

type LedgerData = {
  records: SanctionRecord[];
  summary: { total?: number; active?: number; warnings?: number; timeouts?: number };
};

const ACTION_LABELS: Record<string, string> = {
  WARNING: "경고",
  DELETE: "메시지 삭제",
  TIMEOUT: "타임아웃",
  KICK: "추방",
  BAN: "차단",
};

const STATUS_LABELS: Record<string, string> = {
  active: "적용 중",
  released: "해제",
  expired: "기간 만료",
};

const SOURCE_LABELS: Record<string, string> = {
  review: "검수 카드",
  manual_command: "관리자 수동 등록",
  discord_manual: "Discord 직접 조치",
  automatic: "BB봇 자동 조치",
};

function formatDate(value: string | null) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("ko-KR", {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  }).format(new Date(value));
}

export function StaffLedger() {
  const [authenticated, setAuthenticated] = useState<boolean | null>(null);
  const [password, setPassword] = useState("");
  const [loginError, setLoginError] = useState("");
  const [loading, setLoading] = useState(false);
  const [data, setData] = useState<LedgerData>({ records: [], summary: {} });
  const [status, setStatus] = useState("all");
  const [action, setAction] = useState("all");
  const [query, setQuery] = useState("");

  const load = useCallback(async () => {
    const params = new URLSearchParams({ limit: "100" });
    if (status !== "all") params.set("status", status);
    if (action !== "all") params.set("action", action);
    if (query.trim()) params.set("q", query.trim());
    const response = await fetch(`/api/staff/sanctions?${params}`, { cache: "no-store" });
    if (response.status === 401) {
      setAuthenticated(false);
      return;
    }
    if (!response.ok) throw new Error("ledger unavailable");
    setData(await response.json());
    setAuthenticated(true);
  }, [action, query, status]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      void load().catch(() => setAuthenticated(false));
    }, 0);
    return () => window.clearTimeout(timer);
  }, [load]);

  async function login(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setLoading(true);
    setLoginError("");
    try {
      const response = await fetch("/api/staff/login", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ password }),
      });
      const result = await response.json() as { error?: string; attempts_remaining?: number };
      if (!response.ok) {
        setLoginError(
          result.error === "temporarily_locked"
            ? "로그인 실패가 반복되어 15분 동안 접속이 제한되었습니다."
            : result.error === "staff_login_not_configured"
              ? "아직 관리자 비밀번호가 설정되지 않았습니다."
              : `비밀번호가 올바르지 않습니다.${result.attempts_remaining != null ? ` 남은 시도 ${result.attempts_remaining}회` : ""}`,
        );
        return;
      }
      setPassword("");
      await load();
    } catch {
      setLoginError("로그인 서버에 연결하지 못했습니다. 잠시 뒤 다시 시도해주세요.");
    } finally {
      setLoading(false);
    }
  }

  async function logout() {
    await fetch("/api/staff/logout", { method: "POST" });
    setAuthenticated(false);
    setData({ records: [], summary: {} });
  }

  if (authenticated !== true) {
    return (
      <main className="staff-shell staff-login-shell">
        <section className="staff-login-card">
          <Link className="staff-back-link" href="/">← 운영 KPI</Link>
          <div className="staff-lock-mark">BB</div>
          <p className="eyebrow">STAFF ONLY</p>
          <h1>제재 인수인계 원장</h1>
          <p>경고와 타임아웃의 적용·해제 시각, 사유와 처리자를 확인하는 관리자 전용 화면입니다.</p>
          <form onSubmit={login}>
            <label>
              <span>관리자 비밀번호</span>
              <input
                type="password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                autoComplete="current-password"
                minLength={12}
                maxLength={256}
                required
                autoFocus
              />
            </label>
            {loginError && <p className="staff-login-error" role="alert">{loginError}</p>}
            <button type="submit" disabled={loading}>{loading ? "확인 중…" : "원장 열기"}</button>
          </form>
          <small>5회 연속 실패하면 15분 동안 접속이 제한됩니다. 로그인은 8시간 유지됩니다.</small>
        </section>
      </main>
    );
  }

  return (
    <main className="staff-shell">
      <header className="staff-header">
        <div>
          <p className="eyebrow">STAFF HANDOFF LEDGER</p>
          <h1>제재 인수인계 원장</h1>
          <p>상황 교대 시 적용 중인 조치와 해제 이력을 먼저 확인하세요.</p>
        </div>
        <div className="staff-header-actions">
          <Link href="/">운영 KPI</Link>
          <button type="button" onClick={logout}>로그아웃</button>
        </div>
      </header>

      <section className="staff-summary" aria-label="제재 요약">
        <article><span>전체 기록</span><strong>{Number(data.summary.total ?? 0).toLocaleString("ko-KR")}</strong></article>
        <article className="staff-active"><span>현재 적용 중</span><strong>{Number(data.summary.active ?? 0).toLocaleString("ko-KR")}</strong></article>
        <article><span>경고</span><strong>{Number(data.summary.warnings ?? 0).toLocaleString("ko-KR")}</strong></article>
        <article><span>타임아웃</span><strong>{Number(data.summary.timeouts ?? 0).toLocaleString("ko-KR")}</strong></article>
      </section>

      <section className="staff-toolbar" aria-label="제재 기록 필터">
        <label><span>상태</span><select value={status} onChange={(event) => setStatus(event.target.value)}>
          <option value="all">전체</option><option value="active">적용 중</option>
          <option value="released">해제</option><option value="expired">기간 만료</option>
        </select></label>
        <label><span>조치</span><select value={action} onChange={(event) => setAction(event.target.value)}>
          <option value="all">전체</option><option value="WARNING">경고</option><option value="DELETE">메시지 삭제</option>
          <option value="TIMEOUT">타임아웃</option><option value="KICK">추방</option><option value="BAN">차단</option>
        </select></label>
        <label className="staff-search"><span>검색</span><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="사용자명·Discord ID·사유" /></label>
        <button type="button" onClick={() => void load()}>새로고침</button>
      </section>

      <section className="staff-records" aria-live="polite">
        {data.records.length ? data.records.map((record) => (
          <article className={`staff-record status-${record.status}`} key={record.event_id}>
            <div className="staff-record-topline">
              <div><span className={`staff-action action-${record.action_type.toLowerCase()}`}>{ACTION_LABELS[record.action_type] ?? record.action_type}</span>
                <span className={`staff-status status-${record.status}`}>{STATUS_LABELS[record.status] ?? record.status}</span></div>
              <small>{SOURCE_LABELS[record.source] ?? record.source}</small>
            </div>
            <div className="staff-person"><strong>{record.user_display}</strong><code>{record.user_id}</code></div>
            <p className="staff-reason">{record.reason}</p>
            <dl className="staff-timeline">
              <div><dt>적용</dt><dd>{formatDate(record.issued_at)}<small>{record.issued_by_display ?? "처리자 미확인"}</small></dd></div>
              {record.expires_at && <div><dt>예정 종료</dt><dd>{formatDate(record.expires_at)}</dd></div>}
              <div><dt>해제</dt><dd>{record.released_at ? formatDate(record.released_at) : "아직 해제되지 않음"}<small>{record.released_by_display}</small></dd></div>
            </dl>
            {record.release_reason && <p className="staff-release-reason"><span>해제 사유</span>{record.release_reason}</p>}
          </article>
        )) : <div className="staff-empty"><strong>조건에 맞는 기록이 없습니다.</strong><p>Discord에서 수동 경고를 등록하거나 타임아웃이 적용되면 여기에 표시됩니다.</p></div>}
      </section>
    </main>
  );
}
