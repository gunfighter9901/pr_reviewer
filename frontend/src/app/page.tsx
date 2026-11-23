"use client";

import React, { useCallback, useEffect, useState } from "react";

const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

type SummaryPoint = {
  file_path: string;
  line_start: number;
  line_end: number;
  severity: string;
  category: string;
  agent_type: string;
  confidence: number;
  plain_summary: string;
  evidence_snippet: string;
  suggested_fix?: string | null;
  feedback_id: string | null;
  reviewer_action: string;
};

type PRSummary = {
  repo: string;
  pr_number: number;
  risk_level: string;
  headline: string;
  total_points: number;
  severity_counts: Record<string, number>;
  points: SummaryPoint[];
  markdown: string;
};

type QueueItem = {
  job_id: string;
  delivery_id: string | null;
  repo: string | null;
  pr_number: number | null;
  status: string;
  source: string;
  in_arq_queue: boolean;
  enqueued_at: string | null;
  updated_at: string | null;
  findings_count?: number | null;
  auto_posted?: number | null;
  error?: string | null;
};

type Stats = {
  pending_hitl: number;
  private_pending: number;
  reviews: number;
  findings: number;
  spend_24h_usd: number;
  daily_budget_usd: number;
  confidence_threshold: number;
  spine_path: string;
};

async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(`${API}${path}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`${path} → HTTP ${res.status}`);
  return res.json() as Promise<T>;
}

const RISK_STYLE: Record<string, string> = {
  critical: "bg-red-600 text-white",
  high: "bg-orange-500/20 text-orange-300",
  medium: "bg-amber-500/20 text-amber-300",
  low: "bg-slate-700 text-slate-200",
  info: "bg-slate-800 text-slate-400",
};

export default function DashboardPage() {
  const [tab, setTab] = useState<"hitl" | "queue" | "reviews" | "traces">("hitl");
  const [filter, setFilter] = useState<"pending" | "all">("pending");
  const [stats, setStats] = useState<Stats | null>(null);
  const [prs, setPrs] = useState<PRSummary[]>([]);
  const [queue, setQueue] = useState<{ items: QueueItem[]; counts?: Record<string, number> }>({ items: [] });
  const [reviews, setReviews] = useState<any[]>([]);
  const [traces, setTraces] = useState<any[]>([]);
  const [triggerRepo, setTriggerRepo] = useState("");
  const [triggerPr, setTriggerPr] = useState("");
  const [triggerAutoPost, setTriggerAutoPost] = useState(true);
  const [triggerMsg, setTriggerMsg] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [s, h, q, r, t] = await Promise.all([
        getJSON<Stats>("/api/stats"),
        getJSON<{ items: PRSummary[] }>(
          filter === "pending"
            ? "/api/pr-summaries?reviewer_action=pending&limit=500"
            : "/api/pr-summaries?limit=500"
        ),
        getJSON<{ items: QueueItem[]; counts: Record<string, number> }>("/api/queue?limit=100"),
        getJSON<{ items: any[] }>("/api/reviews?limit=20"),
        getJSON<{ items: any[] }>("/api/traces?limit=100"),
      ]);
      setStats(s);
      setPrs(h.items);
      setQueue({ items: q.items, counts: q.counts });
      setReviews(r.items);
      setTraces(t.items);
    } catch (e: any) {
      setError(e?.message || "Failed to reach backend. Is uvicorn running on :8000?");
    } finally {
      setLoading(false);
    }
  }, [filter]);

  useEffect(() => {
    load();
  }, [load]);

  async function trigger() {
    setTriggerMsg(null);
    const pr = parseInt(triggerPr, 10);
    if (!triggerRepo.includes("/") || !pr || pr < 1) {
      setTriggerMsg("Enter repo as owner/name and a PR number ≥ 1.");
      return;
    }
    try {
      const res = await fetch(`${API}/api/queue/trigger`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ repo: triggerRepo.trim(), pr_number: pr, auto_post: triggerAutoPost }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data?.detail || `HTTP ${res.status}`);
      setTriggerMsg(`✓ ${data.status}: ${data.repo}#${data.pr_number} (${data.job_id}) — worker ${data.auto_post ? "runs and auto-posts" : "dry-runs (no post)"} automatically.`);
      setTriggerRepo("");
      setTriggerPr("");
    } catch (e: any) {
      setTriggerMsg(`✗ ${e?.message || "trigger failed"}`);
    }
    await load();
  }

  async function decide(feedbackId: string, action: string) {
    await fetch(`${API}/api/hitl/${feedbackId}/decision`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action }),
    });
    await load();
  }

  return (
    <div className="mx-auto max-w-6xl px-6 py-8">
      <header className="mb-6 border-b border-slate-800 pb-4">
        <h1 className="text-xl font-bold text-white">PR Reviewer — Live Dashboard</h1>
        <p className="text-sm text-slate-400">
          Backend: <span className="font-mono text-sky-400">{API}</span> · Spine:{" "}
          <span className="font-mono">{stats?.spine_path ?? "…"}</span>
        </p>
        <div className="mt-3 grid grid-cols-4 gap-3 text-center">
          <Stat label="Pending HITL" value={stats?.pending_hitl} />
          <Stat label="Private alerts" value={stats?.private_pending} />
          <Stat label="Reviews" value={stats?.reviews} />
          <Stat
            label="24h spend"
            value={stats ? `$${stats.spend_24h_usd} / $${stats.daily_budget_usd}` : undefined}
          />
        </div>
      </header>

      <div className="mb-4 flex gap-2">
        {(["hitl", "queue", "reviews", "traces"] as const).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`rounded px-4 py-2 text-sm ${tab === t ? "bg-blue-600 text-white" : "bg-slate-900 text-slate-300"}`}
          >
            {t === "hitl" ? `PR reviews (${prs.length})` : t === "queue" ? `Queue (${queue.items.length})` : t === "reviews" ? `History (${reviews.length})` : `Traces (${traces.length})`}
          </button>
        ))}
        <div className="ml-auto flex gap-2">
          {tab === "hitl" && (
            <select
              value={filter}
              onChange={(e) => setFilter(e.target.value as any)}
              className="rounded bg-slate-900 px-2 py-2 text-sm text-slate-200"
            >
              <option value="pending">pending</option>
              <option value="all">all</option>
            </select>
          )}
          <button onClick={load} className="rounded bg-slate-800 px-3 py-2 text-sm text-white">
            Refresh
          </button>
        </div>
      </div>

      {loading && <p className="text-sm text-slate-400">Loading live data from {API}…</p>}
      {error && (
        <div className="rounded border border-red-500/40 bg-red-950/20 p-4 text-sm text-red-200">
          {error}
          <div className="mt-2 font-mono text-xs">Start backend: .venv/bin/uvicorn backend.webhook.app:app --port 8000</div>
        </div>
      )}

      {!loading && !error && tab === "hitl" && (
        prs.length === 0 ? <Empty text="No PRs need review. Run a CLI review to generate findings." /> :
        <div className="space-y-6">
          {prs.map((pr) => (
            <section
              key={`${pr.repo}#${pr.pr_number}`}
              className={`rounded-xl border p-5 ${pr.risk_level === "critical" ? "border-red-500/40 bg-red-950/10" : "border-slate-800 bg-slate-900/60"}`}
            >
              <div className="flex flex-wrap items-center gap-2">
                <span className={`rounded px-2 py-0.5 text-xs font-bold uppercase ${RISK_STYLE[pr.risk_level] || RISK_STYLE.info}`}>
                  {pr.risk_level}
                </span>
                <span className="rounded bg-slate-800 px-2 py-0.5 font-mono text-xs text-slate-200">
                  {pr.repo} #{pr.pr_number}
                </span>
                <span className="font-mono text-xs text-slate-400">
                  {pr.total_points} point(s){Object.entries(pr.severity_counts).map(([s, n]) => ` · ${n} ${s}`).join("")}
                </span>
              </div>
              <p className="mt-2 text-sm text-slate-100">{pr.headline}</p>

              <ol className="mt-4 space-y-4">
                {pr.points.map((p, i) => (
                  <li key={p.feedback_id || `${p.file_path}:${p.line_start}:${i}`} className="rounded-lg border border-slate-800 bg-slate-950/60 p-3">
                    <div className="flex flex-wrap items-center gap-2 text-xs">
                      <span className="font-bold text-slate-300">{i + 1}.</span>
                      <span className={`rounded px-1.5 py-0.5 font-semibold uppercase ${RISK_STYLE[p.severity] || RISK_STYLE.info}`}>
                        {p.severity}
                      </span>
                      <span className="font-mono text-indigo-300">{p.category}</span>
                      <span className="font-mono text-sky-400">
                        {p.file_path}:{p.line_start}{p.line_end > p.line_start ? `-${p.line_end}` : ""}
                      </span>
                      <span className="ml-auto font-mono text-slate-500">
                        {(p.confidence * 100).toFixed(0)}% · {p.agent_type} · {p.reviewer_action}
                      </span>
                    </div>
                    <p className="mt-1.5 text-sm text-slate-200">{p.plain_summary}</p>
                    <pre className="mt-1.5 overflow-x-auto rounded bg-black/40 p-2 font-mono text-xs text-emerald-300">{p.evidence_snippet}</pre>
                    {p.suggested_fix && <div className="mt-1.5 font-mono text-xs text-emerald-200">Fix: {p.suggested_fix}</div>}
                    {p.feedback_id && p.reviewer_action === "pending" && (
                      <div className="mt-2 flex gap-2">
                        <button onClick={() => decide(p.feedback_id as string, "approved")} className="rounded bg-emerald-600 px-3 py-1 text-xs text-white">Approve</button>
                        <button onClick={() => decide(p.feedback_id as string, "edited")} className="rounded bg-sky-600 px-3 py-1 text-xs text-white">Edit & Post</button>
                        <button onClick={() => decide(p.feedback_id as string, "rejected_false_positive")} className="rounded bg-rose-700 px-3 py-1 text-xs text-white">Reject</button>
                      </div>
                    )}
                  </li>
                ))}
              </ol>
            </section>
          ))}
        </div>
      )}

      {!loading && !error && tab === "queue" && (
        <div className="space-y-4">
          <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-4">
            <div className="text-sm font-semibold text-white">Trigger a review manually</div>
            <p className="mt-0.5 text-xs text-slate-400">Same path as GitHub webhooks: enqueues to Redis, the worker fetches the diff and reviews.</p>
            <div className="mt-3 flex flex-wrap gap-2">
              <input
                value={triggerRepo}
                onChange={(e) => setTriggerRepo(e.target.value)}
                placeholder="owner/repo"
                className="w-56 rounded bg-slate-950 px-3 py-2 font-mono text-sm text-slate-100 ring-1 ring-slate-800"
              />
              <input
                value={triggerPr}
                onChange={(e) => setTriggerPr(e.target.value)}
                placeholder="PR #"
                inputMode="numeric"
                className="w-24 rounded bg-slate-950 px-3 py-2 font-mono text-sm text-slate-100 ring-1 ring-slate-800"
              />
              <button onClick={trigger} className="rounded bg-emerald-600 px-4 py-2 text-sm font-semibold text-white">Trigger review</button>
              <label className="flex items-center gap-1.5 text-xs text-slate-300">
                <input
                  type="checkbox"
                  checked={triggerAutoPost}
                  onChange={(e) => setTriggerAutoPost(e.target.checked)}
                  className="h-4 w-4 accent-emerald-600"
                />
                Post to GitHub (off = dry run)
              </label>
            </div>
            {triggerMsg && <div className="mt-2 font-mono text-xs text-slate-300">{triggerMsg}</div>}
          </div>

          {queue.items.length === 0 ? <Empty text="Redis queue is empty. Nothing waiting, nothing recent." /> :
          <table className="w-full text-left text-xs text-slate-200">
            <thead><tr className="border-b border-slate-800 text-slate-400">
              <th className="py-2 pr-4">Job</th><th className="py-2 pr-4">PR</th><th className="py-2 pr-4">Status</th><th className="py-2 pr-4">Result</th><th className="py-2">Enqueued</th>
            </tr></thead>
            <tbody>
              {queue.items.map((j) => (
                <tr key={j.job_id} className="border-b border-slate-800/50 font-mono">
                  <td className="py-2 pr-4 text-slate-400">{j.job_id}{j.in_arq_queue ? " ●" : ""}</td>
                  <td className="py-2 pr-4">{j.repo ? `${j.repo}#${j.pr_number}` : "—"}</td>
                  <td className="py-2 pr-4">
                    <span className={`rounded px-2 py-0.5 uppercase ${j.in_arq_queue ? "bg-amber-500/20 text-amber-300" : j.status === "done" ? "bg-emerald-500/20 text-emerald-300" : j.status === "failed" ? "bg-rose-500/20 text-rose-300" : "bg-slate-800 text-slate-300"}`}>
                      {j.in_arq_queue ? "queued" : j.status}
                    </span>
                    <span className="ml-2 text-slate-500">{j.source}</span>
                  </td>
                  <td className="py-2 pr-4 text-slate-300">
                    {j.findings_count != null ? `${j.findings_count} findings / ${j.auto_posted} posted` : j.error || "—"}
                  </td>
                  <td className="py-2 text-slate-500">{j.enqueued_at ? String(j.enqueued_at).slice(5, 19).replace("T", " ") : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>}
        </div>
      )}

      {!loading && !error && tab === "reviews" && (
        <table className="w-full text-left text-xs text-slate-200">
          <thead><tr className="border-b border-slate-800 text-slate-400">
            <th className="py-2 pr-4">Repo</th><th className="py-2 pr-4">PR</th><th className="py-2 pr-4">Status</th><th className="py-2 pr-4">Findings</th><th className="py-2">Delivery</th>
          </tr></thead>
          <tbody>
            {reviews.map((r: any) => (
              <tr key={r.id} className="border-b border-slate-800/50 font-mono">
                <td className="py-2 pr-4">{r.repo}</td><td className="py-2 pr-4">{r.pr_number}</td>
                <td className="py-2 pr-4">{r.status}</td><td className="py-2 pr-4">{r.finding_count}</td>
                <td className="py-2 text-slate-400">{r.github_delivery_id}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {!loading && !error && tab === "traces" && (
        <table className="w-full text-left text-xs text-slate-200">
          <thead><tr className="border-b border-slate-800 text-slate-400">
            <th className="py-2 pr-4">Time</th><th className="py-2 pr-4">Agent</th><th className="py-2 pr-4">Event</th><th className="py-2 pr-4">Cost</th><th className="py-2">Status</th>
          </tr></thead>
          <tbody>
            {traces.slice().reverse().map((s: any, i: number) => (
              <tr key={i} className="border-b border-slate-800/50 font-mono">
                <td className="py-2 pr-4 text-slate-400">{String(s.time).slice(11, 23)}</td>
                <td className="py-2 pr-4 text-sky-400">{s.agent_name}</td>
                <td className="py-2 pr-4">{s.event_type}</td>
                <td className="py-2 pr-4 text-emerald-400">${Number(s.cost_usd || 0).toFixed(6)}</td>
                <td className="py-2">{s.status}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: any }) {
  return (
    <div className="rounded-lg border border-slate-800 bg-slate-900/70 px-4 py-2.5">
      <div className="text-xs text-slate-400">{label}</div>
      <div className="mt-0.5 font-bold text-white">{value ?? "…"}</div>
    </div>
  );
}

function Empty({ text }: { text: string }) {
  return <div className="rounded border border-slate-800 bg-slate-900/60 p-6 text-sm text-slate-400">{text}</div>;
}
