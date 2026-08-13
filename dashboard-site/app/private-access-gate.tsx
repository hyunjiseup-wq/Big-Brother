"use client";

import Link from "next/link";
import { FormEvent, ReactNode, useEffect, useState } from "react";

export function PrivateAccessGate({ children }: { children: ReactNode }) {
  const [authenticated, setAuthenticated] = useState(false);
  const [checking, setChecking] = useState(true);
  const [password, setPassword] = useState("");
  const [loginError, setLoginError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    void fetch("/api/staff/session", { cache: "no-store", signal: controller.signal })
      .then((response) => setAuthenticated(response.ok))
      .catch(() => setAuthenticated(false))
      .finally(() => setChecking(false));
    return () => controller.abort();
  }, []);

  async function login(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
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
      setAuthenticated(true);
    } catch {
      setLoginError("로그인 서버에 연결하지 못했습니다. 잠시 뒤 다시 시도해주세요.");
    } finally {
      setSubmitting(false);
      setChecking(false);
    }
  }

  async function logout() {
    await fetch("/api/staff/logout", { method: "POST" });
    setAuthenticated(false);
  }

  if (!authenticated) {
    return (
      <main className="staff-shell staff-login-shell">
        <section className="staff-login-card">
          <div className="staff-lock-mark">BB</div>
          <p className="eyebrow">ADMINISTRATORS ONLY</p>
          <h1>운영 대시보드</h1>
          <p>서버 운영진만 열람할 수 있습니다. 전달받은 관리자 공용 비밀번호를 입력하세요.</p>
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
                autoFocus={!checking}
                disabled={checking}
              />
            </label>
            {loginError && <p className="staff-login-error" role="alert">{loginError}</p>}
            <button type="submit" disabled={checking || submitting}>
              {checking ? "세션 확인 중…" : submitting ? "확인 중…" : "대시보드 열기"}
            </button>
          </form>
          <small>5회 연속 실패하면 15분 동안 접속이 제한되며, 로그인은 8시간 유지됩니다.</small>
        </section>
      </main>
    );
  }

  return (
    <>
      <nav className="private-session-bar" aria-label="관리자 메뉴">
        <span>🔒 관리자 인증됨</span>
        <Link href="/staff">제재 인수인계</Link>
        <button type="button" onClick={logout}>로그아웃</button>
      </nav>
      {children}
    </>
  );
}
