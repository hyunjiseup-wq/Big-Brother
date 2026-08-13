"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

type CountRow = {
  label: string;
  count: number;
  confirmed?: number;
  false_positive?: number;
  pending?: number;
};

type DashboardData = {
  range: string;
  period: PeriodSelection & {
    start: number;
    end: number;
    label: string;
  };
  available_periods: {
    years: number[];
    months: string[];
    quarters: string[];
  };
  cards: {
    detected: number;
    confirmed: number;
    false_positive: number;
    pending: number;
    delivery_failed: number;
    resolved: number;
    precision_percent: number | null;
    false_positive_percent: number | null;
    resolution_percent: number | null;
    avg_review_hours: number | null;
  };
  categories: CountRow[];
  channels: CountRow[];
  providers: CountRow[];
  levels: CountRow[];
  languages: CountRow[];
  monthly: CountRow[];
  audit: {
    runs: number;
    reviewed_messages: number;
    flagged_messages: number;
    flag_rate_percent: number | null;
    failed_channels: number;
    target_channels: number;
    successful_channel_percent: number | null;
  };
  operations: null | {
    captured_at: string;
    captured_ts: number;
    pending_over_24h: number;
    pending_over_72h: number;
    active_learning_rules: number;
    ai_retry_queue: number;
    kpi_sync_pending: number;
  };
  freshness: {
    status: "live" | "delayed" | "stored" | "no_data";
    last_sync_at: string | null;
    last_sync_ts: number | null;
    age_seconds: number | null;
    expected_interval_seconds: number;
  };
  updated_at: number | null;
};

type PeriodView = "month" | "quarter" | "year" | "all";

type PeriodSelection = {
  view: PeriodView;
  year: number | null;
  month: number | null;
  quarter: number | null;
};

const VIEW_OPTIONS: ReadonlyArray<readonly [PeriodView, string]> = [
  ["month", "월별"],
  ["quarter", "분기별"],
  ["year", "연간"],
  ["all", "전체"],
] as const;

const MONTH_OPTIONS = Array.from({ length: 12 }, (_, index) => index + 1);
const QUARTER_OPTIONS = [1, 2, 3, 4] as const;
const KST_OFFSET_MS = 9 * 60 * 60 * 1000;

function currentKstSelection(): PeriodSelection & { year: number; month: number; quarter: number } {
  const nowInKst = new Date(Date.now() + KST_OFFSET_MS);
  const month = nowInKst.getUTCMonth() + 1;
  return {
    view: "month",
    year: nowInKst.getUTCFullYear(),
    month,
    quarter: Math.floor((month - 1) / 3) + 1,
  };
}

function selectionFromSearch(search: string, fallback: PeriodSelection): PeriodSelection {
  const params = new URLSearchParams(search);
  const requestedView = params.get("view") as PeriodView | null;
  const view = VIEW_OPTIONS.some(([value]) => value === requestedView) ? requestedView! : fallback.view;
  const requestedYear = Number(params.get("year"));
  const requestedMonth = Number(params.get("month"));
  const requestedQuarter = Number(params.get("quarter"));
  return {
    view,
    year: view === "all" ? null : Number.isInteger(requestedYear) && requestedYear >= 2000 ? requestedYear : fallback.year,
    month: view === "month" && Number.isInteger(requestedMonth) && requestedMonth >= 1 && requestedMonth <= 12
      ? requestedMonth
      : fallback.month,
    quarter: view === "quarter" && Number.isInteger(requestedQuarter) && requestedQuarter >= 1 && requestedQuarter <= 4
      ? requestedQuarter
      : fallback.quarter,
  };
}

function selectionQuery(selection: PeriodSelection) {
  const params = new URLSearchParams({ view: selection.view });
  if (selection.year != null) params.set("year", String(selection.year));
  if (selection.view === "month" && selection.month != null) params.set("month", String(selection.month));
  if (selection.view === "quarter" && selection.quarter != null) params.set("quarter", String(selection.quarter));
  return params.toString();
}

const CATEGORY_LABELS: Record<string, string> = {
  language_etiquette: "말투·욕설·예절",
  rmt_barter: "현금거래·물물교환",
  ads_links_invites: "홍보·링크·초대",
  conflict_mockery: "갈등·저격·조롱",
  cheat: "핵·치트",
  politics: "정치",
  platform_safety: "플랫폼 안전",
  other_complex: "기타·복합",
};

const LEVEL_LABELS: Record<string, string> = {
  MINOR: "경미",
  MODERATE: "보통",
  SEVERE: "심각",
  EXTREME: "최고 위험",
};

const LANGUAGE_LABELS: Record<string, string> = {
  ko: "한국어",
  latin: "라틴 문자",
  mixed: "혼합 언어",
  und: "짧은 문장·판별 불가",
  ja: "일본어",
  zh: "중국어",
};

function formatNumber(value: number | null | undefined) {
  return Number(value ?? 0).toLocaleString("ko-KR");
}

function formatPercent(value: number | null | undefined) {
  return value == null ? "—" : `${value.toFixed(1)}%`;
}

function formatTime(value: number | null) {
  if (!value) return "아직 수집 전";
  return new Intl.DateTimeFormat("ko-KR", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function formatAge(seconds: number | null) {
  if (seconds == null) return "연결 기록 없음";
  if (seconds < 60) return "방금 전";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}분 전`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}시간 전`;
  return `${Math.floor(hours / 24)}일 전`;
}

const FRESHNESS_COPY = {
  live: {
    label: "실시간 동기화",
    message: "봇 연결이 정상이며 5분 간격으로 운영 지표를 저장하고 있습니다.",
  },
  delayed: {
    label: "동기화 지연",
    message: "최근 동기화가 평소보다 늦습니다. 봇 또는 네트워크 상태를 확인하세요.",
  },
  stored: {
    label: "저장 데이터 표시",
    message: "수집이 30분 이상 멈춰 마지막으로 저장된 집계를 표시하고 있습니다.",
  },
  no_data: {
    label: "연결 대기",
    message: "아직 봇의 운영 상태가 수집되지 않았습니다.",
  },
} as const;

function rankBy(rows: CountRow[], key: "confirmed" | "false_positive") {
  return [...rows].sort((a, b) => Number(b[key] ?? 0) - Number(a[key] ?? 0))[0];
}

function MetricCard({
  label,
  value,
  note,
  tone = "neutral",
}: {
  label: string;
  value: string;
  note: string;
  tone?: "neutral" | "good" | "warn" | "bad";
}) {
  return (
    <article className={`metric-card tone-${tone}`}>
      <span className="metric-label">{label}</span>
      <strong>{value}</strong>
      <small>{note}</small>
    </article>
  );
}

function Distribution({ rows, labels }: { rows: CountRow[]; labels: Record<string, string> }) {
  const maximum = Math.max(...rows.map((row) => Number(row.count)), 1);
  return (
    <div className="distribution-list">
      {rows.length ? rows.map((row) => (
        <div className="distribution-row" key={row.label}>
          <div className="distribution-copy">
            <span>{labels[row.label] ?? row.label}</span>
            <strong>{formatNumber(row.count)}건</strong>
          </div>
          <div className="bar-track" aria-label={`${labels[row.label] ?? row.label} ${row.count}건`}>
            <span className="bar-fill" style={{ width: `${Math.max(3, Number(row.count) / maximum * 100)}%` }} />
          </div>
        </div>
      )) : <p className="empty-copy">선택한 기간의 데이터가 없습니다.</p>}
    </div>
  );
}

export function Dashboard({ initialSearch = "" }: { initialSearch?: string }) {
  const [selection, setSelection] = useState<PeriodSelection>(() => {
    const current = currentKstSelection();
    return selectionFromSearch(initialSearch, current);
  });
  const [data, setData] = useState<DashboardData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  const load = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true);
    try {
      const response = await fetch(`/api/dashboard?${selectionQuery(selection)}`, {
        cache: "no-store",
      });
      if (!response.ok) throw new Error("dashboard unavailable");
      setData(await response.json());
      setError(false);
    } catch {
      setError(true);
    } finally {
      setLoading(false);
    }
  }, [selection]);

  useEffect(() => {
    window.history.replaceState(null, "", `${window.location.pathname}?${selectionQuery(selection)}`);
    const initialTimer = window.setTimeout(() => void load(), 0);
    const timer = window.setInterval(() => load(true), 60_000);
    return () => {
      window.clearTimeout(initialTimer);
      window.clearInterval(timer);
    };
  }, [load, selection]);

  const topConfirmedChannel = useMemo(
    () => data ? rankBy(data.channels, "confirmed") : undefined,
    [data],
  );
  const topFalsePositiveChannel = useMemo(
    () => data ? rankBy(data.channels, "false_positive") : undefined,
    [data],
  );
  const maxMonth = Math.max(...(data?.monthly ?? []).map((row) => Number(row.count)), 1);
  const freshness = data?.freshness ?? {
    status: "no_data" as const,
    last_sync_at: null,
    last_sync_ts: null,
    age_seconds: null,
    expected_interval_seconds: 300,
  };
  const freshnessCopy = FRESHNESS_COPY[freshness.status];
  const current = currentKstSelection();
  const yearOptions = [...new Set([
    selection.year ?? current.year,
    ...(data?.available_periods.years ?? []),
    current.year,
  ])].sort((a, b) => b - a);
  const selectedMonthKey = selection.year && selection.month
    ? `${selection.year}-${String(selection.month).padStart(2, "0")}`
    : null;
  const selectedQuarterKey = selection.year && selection.quarter
    ? `${selection.year}-Q${selection.quarter}`
    : null;

  return (
    <main>
      <header className="topbar">
        <div className="brand-lockup">
          <span className="brand-mark">BB</span>
          <div>
            <p>BIG BROTHER AUTOMOD</p>
            <h1>운영 인사이트</h1>
          </div>
        </div>
        <div className="sync-state" title="봇이 꺼져 있어도 마지막 저장 데이터는 유지됩니다.">
          <span className={`status-dot ${freshness.status}`} />
          <div>
            <strong>{freshnessCopy.label}</strong>
            <small>{formatAge(freshness.age_seconds)} 동기화</small>
          </div>
        </div>
      </header>

      <section className="hero">
        <div>
          <p className="eyebrow">관리자 검수 결과 기반</p>
          <h2>감지의 양보다<br /><em>판단의 질</em>을 봅니다.</h2>
          <p className="hero-copy">
            카드 감지부터 관리자 확정, 오탐 학습과 배치 감사까지 한 흐름으로 확인하세요.
          </p>
        </div>
        <div className="period-control" aria-label="조회 기간">
          <label className="period-field">
            <span>조회 단위</span>
            <select
              aria-label="조회 단위"
              value={selection.view}
              onChange={(event) => setSelection((currentSelection) => ({
                ...currentSelection,
                view: event.target.value as PeriodView,
                year: event.target.value === "all" ? null : currentSelection.year ?? current.year,
              }))}
            >
              {VIEW_OPTIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select>
          </label>
          {selection.view !== "all" && (
            <label className="period-field">
              <span>연도</span>
              <select
                aria-label="조회 연도"
                value={selection.year ?? current.year}
                onChange={(event) => setSelection((currentSelection) => ({
                  ...currentSelection,
                  year: Number(event.target.value),
                }))}
              >
                {yearOptions.map((year) => <option key={year} value={year}>{year}년</option>)}
              </select>
            </label>
          )}
          {selection.view === "month" && (
            <label className="period-field">
              <span>월</span>
              <select
                aria-label="조회 월"
                value={selection.month ?? current.month}
                onChange={(event) => setSelection((currentSelection) => ({
                  ...currentSelection,
                  month: Number(event.target.value),
                }))}
              >
                {MONTH_OPTIONS.map((month) => (
                  <option key={month} value={month}>
                    {month}월{data && selection.year && !data.available_periods.months.includes(`${selection.year}-${String(month).padStart(2, "0")}`) ? " · 자료 없음" : ""}
                  </option>
                ))}
              </select>
            </label>
          )}
          {selection.view === "quarter" && (
            <label className="period-field">
              <span>분기</span>
              <select
                aria-label="조회 분기"
                value={selection.quarter ?? current.quarter}
                onChange={(event) => setSelection((currentSelection) => ({
                  ...currentSelection,
                  quarter: Number(event.target.value),
                }))}
              >
                {QUARTER_OPTIONS.map((quarter) => (
                  <option key={quarter} value={quarter}>
                    {quarter}분기{data && selection.year && !data.available_periods.quarters.includes(`${selection.year}-Q${quarter}`) ? " · 자료 없음" : ""}
                  </option>
                ))}
              </select>
            </label>
          )}
          <div className="period-summary" aria-live="polite">
            <span>선택 기간</span>
            <strong>{data?.period.label ?? "불러오는 중"}</strong>
            {data && selection.view === "month" && selectedMonthKey && !data.available_periods.months.includes(selectedMonthKey) && <small>수집 자료 없음</small>}
            {data && selection.view === "quarter" && selectedQuarterKey && !data.available_periods.quarters.includes(selectedQuarterKey) && <small>수집 자료 없음</small>}
          </div>
        </div>
      </section>

      {data && (
        <section className={`sync-banner sync-${freshness.status}`} aria-live="polite">
          <div>
            <strong>{freshnessCopy.label}</strong>
            <p>{freshnessCopy.message}</p>
          </div>
          <div className="sync-details">
            <span>마지막 동기화 {formatTime(freshness.last_sync_ts)}</span>
            {data.operations && data.operations.kpi_sync_pending > 0 && (
              <strong>미전송 KPI {formatNumber(data.operations.kpi_sync_pending)}건</strong>
            )}
          </div>
        </section>
      )}

      {error && (
        <div className="notice error-notice">
          저장된 집계를 불러오지 못했습니다. 잠시 뒤 자동으로 다시 시도합니다.
          <button type="button" onClick={() => load()}>지금 다시 시도</button>
        </div>
      )}

      {data && data.cards.detected === 0 && data.audit.runs === 0 && (
        <div className="notice empty-period-notice">
          <strong>{data.period.label}</strong>에는 수집된 감지 카드나 감사 기록이 없습니다.
        </div>
      )}

      {loading && !data ? (
        <div className="loading-state" role="status">
          <span />
          <p>운영 지표를 정리하고 있습니다.</p>
        </div>
      ) : data && (
        <>
          <section className="metrics-grid" aria-label="핵심 KPI">
            <MetricCard label="감지 카드" value={`${formatNumber(data.cards.detected)}건`} note="선택 기간 전체 감지" />
            <MetricCard label="정탐" value={`${formatNumber(data.cards.confirmed)}건`} note={`정밀도 ${formatPercent(data.cards.precision_percent)}`} tone="good" />
            <MetricCard label="오탐" value={`${formatNumber(data.cards.false_positive)}건`} note={`오탐률 ${formatPercent(data.cards.false_positive_percent)}`} tone="bad" />
            <MetricCard label="미검수" value={`${formatNumber(data.cards.pending)}건`} note={`해결률 ${formatPercent(data.cards.resolution_percent)}`} tone={data.cards.pending ? "warn" : "neutral"} />
          </section>

          <section className="insight-strip">
            <div className="insight-lead">
              <span>이번 기간 핵심</span>
              <strong>{CATEGORY_LABELS[data.categories[0]?.label] ?? data.categories[0]?.label ?? "아직 감지 없음"}</strong>
              <p>가장 많이 감지된 유형 · {formatNumber(data.categories[0]?.count)}건</p>
            </div>
            <div className="insight-item">
              <span>정탐 최다 채널</span>
              <strong>{Number(topConfirmedChannel?.confirmed ?? 0) ? topConfirmedChannel?.label : "—"}</strong>
              <small>{formatNumber(topConfirmedChannel?.confirmed)}건</small>
            </div>
            <div className="insight-item">
              <span>오탐 최다 채널</span>
              <strong>{Number(topFalsePositiveChannel?.false_positive ?? 0) ? topFalsePositiveChannel?.label : "—"}</strong>
              <small>{formatNumber(topFalsePositiveChannel?.false_positive)}건</small>
            </div>
            <div className="insight-item">
              <span>평균 검수 시간</span>
              <strong>{data.cards.avg_review_hours == null ? "—" : `${data.cards.avg_review_hours}시간`}</strong>
              <small>관리자 확정까지</small>
            </div>
          </section>

          <section className="dashboard-grid">
            <article className="panel trend-panel">
              <div className="panel-heading">
                <div><span>12개월 흐름</span><h3>감지 품질 추세</h3></div>
                <span className="legend"><i className="tp" />정탐 <i className="fp" />오탐</span>
              </div>
              <div className="month-chart">
                {data.monthly.length ? data.monthly.map((row) => {
                  const height = Math.max(8, Number(row.count) / maxMonth * 100);
                  const confirmedRatio = row.count ? Number(row.confirmed ?? 0) / Number(row.count) * 100 : 0;
                  return (
                    <div className="month-column" key={row.label} title={`${row.label}: ${row.count}건`}>
                      <div className="month-value">{row.count}</div>
                      <div className="month-bar" style={{ height: `${height}%` }}>
                        <span className="month-confirmed" style={{ height: `${confirmedRatio}%` }} />
                      </div>
                      <small>{row.label.slice(5)}월</small>
                    </div>
                  );
                }) : <p className="empty-copy">월별 추세가 쌓이면 여기에 표시됩니다.</p>}
              </div>
            </article>

            <article className="panel category-panel">
              <div className="panel-heading"><div><span>탐지 구성</span><h3>유형별 감지</h3></div></div>
              <Distribution rows={data.categories} labels={CATEGORY_LABELS} />
            </article>

            <article className="panel channel-panel">
              <div className="panel-heading"><div><span>집중 구간</span><h3>채널별 정확도</h3></div></div>
              <div className="table-wrap">
                <table>
                  <thead><tr><th>채널</th><th>감지</th><th>정탐</th><th>오탐</th><th>정밀도</th></tr></thead>
                  <tbody>
                    {data.channels.map((row) => {
                      const resolved = Number(row.confirmed ?? 0) + Number(row.false_positive ?? 0);
                      const precision = resolved ? Number(row.confirmed ?? 0) / resolved * 100 : null;
                      return (
                        <tr key={row.label}>
                          <td>{row.label}</td>
                          <td>{formatNumber(row.count)}</td>
                          <td className="text-good">{formatNumber(row.confirmed)}</td>
                          <td className="text-bad">{formatNumber(row.false_positive)}</td>
                          <td>{formatPercent(precision)}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </article>

            <article className="panel provider-panel">
              <div className="panel-heading"><div><span>판단망 비교</span><h3>AI 제공자 성과</h3></div></div>
              <div className="provider-list">
                {data.providers.map((row) => {
                  const resolved = Number(row.confirmed ?? 0) + Number(row.false_positive ?? 0);
                  const precision = resolved ? Number(row.confirmed ?? 0) / resolved * 100 : 0;
                  return (
                    <div className="provider-row" key={row.label}>
                      <span className="provider-name">{row.label}</span>
                      <div className="provider-meter"><span style={{ width: `${Math.max(2, precision)}%` }} /></div>
                      <strong>{precision.toFixed(1)}%</strong>
                      <small>{formatNumber(row.count)}건</small>
                    </div>
                  );
                })}
                {!data.providers.length && <p className="empty-copy">제공자 데이터가 없습니다.</p>}
              </div>
            </article>

            <article className="panel audit-panel">
              <div className="panel-heading"><div><span>감시 커버리지</span><h3>배치 감사</h3></div></div>
              <div className="audit-main">
                <strong>{formatNumber(data.audit.reviewed_messages)}</strong><span>개 메시지 검토</span>
              </div>
              <div className="audit-stats">
                <div><span>실행</span><strong>{data.audit.runs}회</strong></div>
                <div><span>의심 감지율</span><strong>{formatPercent(data.audit.flag_rate_percent)}</strong></div>
                <div><span>채널 성공률</span><strong>{formatPercent(data.audit.successful_channel_percent)}</strong></div>
              </div>
            </article>

            <article className="panel health-panel">
              <div className="panel-heading"><div><span>마지막 저장 상태</span><h3>운영 건전성</h3></div></div>
              {data.operations ? (
                <div className="health-list">
                  <div><span>24시간 초과 미검수</span><strong className={data.operations.pending_over_24h ? "text-bad" : "text-good"}>{data.operations.pending_over_24h}건</strong></div>
                  <div><span>72시간 초과 미검수</span><strong className={data.operations.pending_over_72h ? "text-bad" : "text-good"}>{data.operations.pending_over_72h}건</strong></div>
                  <div><span>AI 장애 재판단 대기</span><strong>{data.operations.ai_retry_queue}건</strong></div>
                  <div><span>활성 오탐 학습 규칙</span><strong>{formatNumber(data.operations.active_learning_rules)}개</strong></div>
                </div>
              ) : <p className="empty-copy">봇이 연결되면 마지막 운영 상태가 저장됩니다.</p>}
            </article>

            <article className="panel compact-panel">
              <div className="panel-heading"><div><span>언어 분포</span><h3>판단 입력 특성</h3></div></div>
              <Distribution rows={data.languages.slice(0, 6)} labels={LANGUAGE_LABELS} />
            </article>

            <article className="panel compact-panel">
              <div className="panel-heading"><div><span>위험도 분포</span><h3>감지 등급</h3></div></div>
              <Distribution rows={data.levels.slice(0, 6)} labels={LEVEL_LABELS} />
            </article>
          </section>
        </>
      )}

      <footer>
        <span>BB AUTOMOD · OPERATIONS</span>
        <p>모든 정탐·오탐은 관리자 검수 결과 기준이며 사용자와 메시지 원문은 저장하지 않습니다.</p>
      </footer>
    </main>
  );
}
