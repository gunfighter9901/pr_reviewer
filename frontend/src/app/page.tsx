"use client";

import React, { useState } from "react";

export interface FindingContract {
  id: string;
  feedback_id: string;
  delivery_id: string;
  repo: string;
  pr_number: number;
  agent_type: "security" | "quality" | "testing" | "docs";
  severity: "critical" | "high" | "medium" | "low" | "info";
  category: string;
  file_path: string;
  line_start: number;
  line_end: number;
  evidence_snippet: string;
  rationale: string;
  suggested_fix?: string;
  confidence: number;
  prompt_version: string;
  routing_decision: "auto_post" | "hitl_escalate" | "private_security_alert";
  private_alert: boolean;
  reviewer_action: "pending" | "approved" | "edited" | "rejected_false_positive";
  decay_weight: number;
  evidence_count: number;
}

export interface TraceSpan {
  time: string;
  trace_id: string;
  delivery_id: string;
  agent_name: string;
  event_type: string;
  model: string;
  prompt_version: string;
  tokens_in: number;
  tokens_out: number;
  cost_usd: number;
  latency_ms: number;
  status: "ok" | "error" | "rejected";
}

const INITIAL_HITL_QUEUE: FindingContract[] = [
  {
    id: "fnd-sec-001",
    feedback_id: "hitl-901",
    delivery_id: "deliv-gh-8821",
    repo: "acme-corp/payment-gateway",
    pr_number: 101,
    agent_type: "security",
    severity: "critical",
    category: "sql_injection",
    file_path: "backend/payments/query.py",
    line_start: 16,
    line_end: 17,
    evidence_snippet: "sql = f\"SELECT * FROM ledger WHERE account_id = '{account_id}'\"",
    rationale:
      "[security] Unsanitized f-string interpolation directly into SQL query string allows arbitrary SQL injection on a public repository. Withheld from public GitHub thread per Security Vulnerability Rule.",
    suggested_fix:
      "cursor.execute(\"SELECT * FROM ledger WHERE account_id = $1\", (account_id,))",
    confidence: 0.96,
    prompt_version: "security@v1.2.0",
    routing_decision: "private_security_alert",
    private_alert: true,
    reviewer_action: "pending",
    decay_weight: 1.0,
    evidence_count: 1,
  },
  {
    id: "fnd-qual-002",
    feedback_id: "hitl-902",
    delivery_id: "deliv-gh-8825",
    repo: "acme-corp/user-profile",
    pr_number: 105,
    agent_type: "quality",
    severity: "medium",
    category: "null_dereference",
    file_path: "src/profile/normalizer.py",
    line_start: 10,
    line_end: 10,
    evidence_snippet: "return payload.get(\"display_name\").strip()",
    rationale:
      "Chaining .strip() directly onto dict.get() without a fallback string raises AttributeError when 'display_name' is missing. Escalated because confidence (0.68) < threshold (0.75).",
    suggested_fix: "return (payload.get(\"display_name\") or \"\").strip()",
    confidence: 0.68,
    prompt_version: "quality@v1.2.0",
    routing_decision: "hitl_escalate",
    private_alert: false,
    reviewer_action: "pending",
    decay_weight: 0.85,
    evidence_count: 2,
  },
  {
    id: "fnd-docs-003",
    feedback_id: "hitl-903",
    delivery_id: "deliv-gh-8825",
    repo: "acme-corp/user-profile",
    pr_number: 105,
    agent_type: "docs",
    severity: "info",
    category: "untracked_todo_comment",
    file_path: "src/profile/normalizer.py",
    line_start: 9,
    line_end: 9,
    evidence_snippet: "# TODO: handle locale overrides",
    rationale:
      "Inline TODO comment lacks an issue tracker reference. Routed to HITL queue as confidence (0.65) < threshold (0.75).",
    suggested_fix: "# TODO(#412): handle locale overrides",
    confidence: 0.65,
    prompt_version: "docs@v1.1.0",
    routing_decision: "hitl_escalate",
    private_alert: false,
    reviewer_action: "pending",
    decay_weight: 0.85,
    evidence_count: 3,
  },
];

const INITIAL_TRACES: TraceSpan[] = [
  {
    time: "2026-10-19T09:00:01.012Z",
    trace_id: "tr-8821-a9f2",
    delivery_id: "deliv-gh-8821",
    agent_name: "ingress",
    event_type: "webhook_queued",
    model: "system",
    prompt_version: "v1.0.0",
    tokens_in: 0,
    tokens_out: 0,
    cost_usd: 0.0,
    latency_ms: 14,
    status: "ok",
  },
  {
    time: "2026-10-19T09:00:01.068Z",
    trace_id: "tr-8821-a9f2",
    delivery_id: "deliv-gh-8821",
    agent_name: "retriever",
    event_type: "hybrid_context_retrieved",
    model: "text-embedding-3-small",
    prompt_version: "v1.0.0",
    tokens_in: 420,
    tokens_out: 0,
    cost_usd: 0.000008,
    latency_ms: 48,
    status: "ok",
  },
  {
    time: "2026-10-19T09:00:01.690Z",
    trace_id: "tr-8821-a9f2",
    delivery_id: "deliv-gh-8821",
    agent_name: "security",
    event_type: "specialist_llm_call",
    model: "claude-3-5-sonnet",
    prompt_version: "security@v1.2.0",
    tokens_in: 1850,
    tokens_out: 390,
    cost_usd: 0.0114,
    latency_ms: 612,
    status: "ok",
  },
  {
    time: "2026-10-19T09:00:01.640Z",
    trace_id: "tr-8821-a9f2",
    delivery_id: "deliv-gh-8821",
    agent_name: "quality",
    event_type: "specialist_llm_call",
    model: "gpt-4o",
    prompt_version: "quality@v1.2.0",
    tokens_in: 1720,
    tokens_out: 310,
    cost_usd: 0.0074,
    latency_ms: 560,
    status: "ok",
  },
  {
    time: "2026-10-19T09:00:01.410Z",
    trace_id: "tr-8821-a9f2",
    delivery_id: "deliv-gh-8821",
    agent_name: "testing",
    event_type: "specialist_llm_call",
    model: "claude-3-5-haiku",
    prompt_version: "testing@v1.1.0",
    tokens_in: 1410,
    tokens_out: 220,
    cost_usd: 0.002008,
    latency_ms: 335,
    status: "ok",
  },
  {
    time: "2026-10-19T09:00:01.350Z",
    trace_id: "tr-8821-a9f2",
    delivery_id: "deliv-gh-8821",
    agent_name: "docs",
    event_type: "specialist_llm_call",
    model: "gpt-4o-mini",
    prompt_version: "docs@v1.1.0",
    tokens_in: 1290,
    tokens_out: 180,
    cost_usd: 0.000302,
    latency_ms: 280,
    status: "ok",
  },
  {
    time: "2026-10-19T09:00:01.715Z",
    trace_id: "tr-8821-a9f2",
    delivery_id: "deliv-gh-8821",
    agent_name: "aggregator",
    event_type: "fan_in_completed",
    model: "system",
    prompt_version: "v1.0.0",
    tokens_in: 0,
    tokens_out: 0,
    cost_usd: 0.0,
    latency_ms: 9,
    status: "ok",
  },
  {
    time: "2026-10-19T09:00:01.730Z",
    trace_id: "tr-8821-a9f2",
    delivery_id: "deliv-gh-8821",
    agent_name: "hitl_gate",
    event_type: "gate_routing_completed",
    model: "system",
    prompt_version: "v1.0.0",
    tokens_in: 0,
    tokens_out: 0,
    cost_usd: 0.0,
    latency_ms: 11,
    status: "ok",
  },
];

export default function OperationalDashboardPage() {
  const [activeTab, setActiveTab] = useState<"hitl" | "traces" | "economics">(
    "hitl"
  );
  const [hitlItems, setHitlItems] = useState<FindingContract[]>(INITIAL_HITL_QUEUE);

  const handleDecision = (
    feedbackId: string,
    action: "approved" | "edited" | "rejected_false_positive"
  ) => {
    setHitlItems((prev) =>
      prev.map((item) => {
        if (item.feedback_id !== feedbackId) return item;
        const nextCount = item.evidence_count + 1;
        const nextDecay =
          action === "rejected_false_positive" && nextCount >= 3
            ? Number((item.decay_weight * 0.85).toFixed(4))
            : item.decay_weight;
        return {
          ...item,
          reviewer_action: action,
          evidence_count: nextCount,
          decay_weight: nextDecay,
        };
      })
    );
  };

  const pendingCount = hitlItems.filter((i) => i.reviewer_action === "pending").length;
  const privateSecurityCount = hitlItems.filter((i) => i.private_alert).length;
  const totalSpendUsd = INITIAL_TRACES.reduce((acc, s) => acc + s.cost_usd, 0);
  const dailyBudgetUsd = 10.0;

  return (
    <div className="mx-auto max-w-7xl px-6 py-8">
      <header className="mb-8 flex flex-col justify-between gap-4 border-b border-slate-800 pb-6 md:flex-row md:items-center">
        <div>
          <div className="flex items-center gap-3">
            <span className="rounded bg-emerald-500/10 px-2.5 py-1 text-xs font-semibold uppercase tracking-wider text-emerald-400 ring-1 ring-emerald-500/30">
              L0 Selectivity Over Coverage
            </span>
            <span className="rounded bg-blue-500/10 px-2.5 py-1 text-xs font-mono text-blue-400 ring-1 ring-blue-500/30">
              Tiger Data Spine + LangGraph
            </span>
          </div>
          <h1 className="mt-2 text-2xl font-bold tracking-tight text-white">
            Multi-Agent PR Reviewer — Operational Harness
          </h1>
          <p className="mt-1 text-sm text-slate-400">
            HITL Escalation Queue, Append-Only Hypertable Trace Spine, and Real-Time Token Budget Guard
          </p>
        </div>

        <div className="grid grid-cols-3 gap-3 text-center">
          <div className="rounded-lg border border-slate-800 bg-slate-900/70 px-4 py-2.5">
            <div className="text-xs text-slate-400">Pending HITL</div>
            <div className="mt-0.5 text-xl font-bold text-amber-400">{pendingCount}</div>
          </div>
          <div className="rounded-lg border border-red-900/50 bg-red-950/20 px-4 py-2.5">
            <div className="text-xs text-red-300">Private Sec Alerts</div>
            <div className="mt-0.5 text-xl font-bold text-red-400">
              {privateSecurityCount}
            </div>
          </div>
          <div className="rounded-lg border border-slate-800 bg-slate-900/70 px-4 py-2.5">
            <div className="text-xs text-slate-400">24h Spend / Limit</div>
            <div className="mt-0.5 text-xl font-bold text-emerald-400">
              ${totalSpendUsd.toFixed(4)} / ${dailyBudgetUsd.toFixed(2)}
            </div>
          </div>
        </div>
      </header>

      <nav className="mb-6 flex gap-2 border-b border-slate-800 pb-3">
        <button
          onClick={() => setActiveTab("hitl")}
          className={`rounded-md px-4 py-2 text-sm font-medium transition ${
            activeTab === "hitl"
              ? "bg-blue-600 text-white"
              : "bg-slate-900 text-slate-300 hover:bg-slate-800"
          }`}
        >
          1. HITL Approval &amp; Security Queue ({hitlItems.length})
        </button>
        <button
          onClick={() => setActiveTab("traces")}
          className={`rounded-md px-4 py-2 text-sm font-medium transition ${
            activeTab === "traces"
              ? "bg-blue-600 text-white"
              : "bg-slate-900 text-slate-300 hover:bg-slate-800"
          }`}
        >
          2. End-to-End Trace Viewer ({INITIAL_TRACES.length} Spans)
        </button>
        <button
          onClick={() => setActiveTab("economics")}
          className={`rounded-md px-4 py-2 text-sm font-medium transition ${
            activeTab === "economics"
              ? "bg-blue-600 text-white"
              : "bg-slate-900 text-slate-300 hover:bg-slate-800"
          }`}
        >
          3. LLM Economics &amp; Continuous Aggregates
        </button>
      </nav>

      {activeTab === "hitl" && (
        <section className="space-y-4">
          {hitlItems.map((item) => (
            <div
              key={item.id}
              className={`rounded-xl border p-5 ${
                item.private_alert
                  ? "border-red-500/40 bg-red-950/10"
                  : "border-slate-800 bg-slate-900/60"
              }`}
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex flex-wrap items-center gap-2">
                  {item.private_alert && (
                    <span className="rounded bg-red-600 px-2 py-0.5 text-xs font-bold uppercase text-white">
                      Private Security Alert — Withheld From Public PR
                    </span>
                  )}
                  <span className="rounded bg-slate-800 px-2 py-0.5 text-xs font-mono text-slate-200">
                    {item.repo} #{item.pr_number}
                  </span>
                  <span className="rounded bg-indigo-500/20 px-2 py-0.5 text-xs font-semibold uppercase text-indigo-300">
                    {item.agent_type}
                  </span>
                  <span className="rounded bg-amber-500/20 px-2 py-0.5 text-xs font-semibold uppercase text-amber-300">
                    {item.severity} · {item.category}
                  </span>
                  <span className="text-xs font-mono text-slate-400">
                    Confidence: {(item.confidence * 100).toFixed(0)}% | Prompt:{" "}
                    {item.prompt_version}
                  </span>
                </div>

                <span
                  className={`rounded px-2.5 py-0.5 text-xs font-semibold uppercase ${
                    item.reviewer_action === "pending"
                      ? "bg-amber-500/20 text-amber-300"
                      : item.reviewer_action === "approved" ||
                        item.reviewer_action === "edited"
                      ? "bg-emerald-500/20 text-emerald-300"
                      : "bg-rose-500/20 text-rose-300"
                  }`}
                >
                  Status: {item.reviewer_action}
                </span>
              </div>

              <div className="mt-3 font-mono text-xs text-slate-300">
                File:{" "}
                <span className="text-sky-400">
                  {item.file_path}:{item.line_start}-{item.line_end}
                </span>
              </div>

              <pre className="mt-2 overflow-x-auto rounded-lg border border-slate-800 bg-slate-950 p-3 font-mono text-xs text-emerald-300">
                {item.evidence_snippet}
              </pre>

              <p className="mt-3 text-sm text-slate-200">
                <strong className="text-white">Rationale:</strong> {item.rationale}
              </p>

              {item.suggested_fix && (
                <div className="mt-2 rounded border border-emerald-900/40 bg-emerald-950/20 p-2.5 font-mono text-xs text-emerald-200">
                  <strong>Suggested Fix:</strong> {item.suggested_fix}
                </div>
              )}

              <div className="mt-4 flex flex-wrap items-center justify-between gap-4 border-t border-slate-800/80 pt-3">
                <div className="text-xs text-slate-400">
                  Procedural Decay Weight:{" "}
                  <span className="font-mono text-slate-200">
                    {item.decay_weight}
                  </span>{" "}
                  | Verified Evidence Count:{" "}
                  <span className="font-mono text-slate-200">
                    {item.evidence_count} / 3 (Anti-Poisoning Gate)
                  </span>
                </div>

                <div className="flex gap-2">
                  <button
                    onClick={() => handleDecision(item.feedback_id, "approved")}
                    className="rounded bg-emerald-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-emerald-500"
                  >
                    Approve Finding
                  </button>
                  <button
                    onClick={() => handleDecision(item.feedback_id, "edited")}
                    className="rounded bg-sky-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-sky-500"
                  >
                    Edit &amp; Post
                  </button>
                  <button
                    onClick={() =>
                      handleDecision(
                        item.feedback_id,
                        "rejected_false_positive"
                      )
                    }
                    className="rounded bg-rose-700 px-3 py-1.5 text-xs font-semibold text-white hover:bg-rose-600"
                  >
                    Reject False Positive
                  </button>
                </div>
              </div>
            </div>
          ))}
        </section>
      )}

      {activeTab === "traces" && (
        <section className="rounded-xl border border-slate-800 bg-slate-900/60 p-5">
          <div className="mb-4 flex items-center justify-between">
            <div>
              <h2 className="text-lg font-semibold text-white">
                End-to-End Immutable Hypertable Trace Spine (`agent_events`)
              </h2>
              <p className="text-xs text-slate-400">
                Protected by PostgreSQL/TimescaleDB BEFORE UPDATE OR DELETE and BEFORE TRUNCATE triggers (INV-4)
              </p>
            </div>
            <span className="rounded bg-slate-800 px-3 py-1 font-mono text-xs text-sky-400">
              Trace: tr-8821-a9f2 | Delivery: deliv-gh-8821
            </span>
          </div>

          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-left text-xs">
              <thead>
                <tr className="border-b border-slate-800 text-slate-400">
                  <th className="py-2.5 pr-4">Timestamp</th>
                  <th className="py-2.5 pr-4">Node / Agent</th>
                  <th className="py-2.5 pr-4">Event Type</th>
                  <th className="py-2.5 pr-4">Routed Model</th>
                  <th className="py-2.5 pr-4">Prompt Version</th>
                  <th className="py-2.5 pr-4">Tokens (In/Out)</th>
                  <th className="py-2.5 pr-4">Cost (USD)</th>
                  <th className="py-2.5 pr-4">Latency</th>
                  <th className="py-2.5">Status</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/70 font-mono">
                {INITIAL_TRACES.map((span, idx) => (
                  <tr key={idx} className="hover:bg-slate-800/40">
                    <td className="py-2.5 pr-4 text-slate-400">
                      {span.time.slice(11, 23)}
                    </td>
                    <td className="py-2.5 pr-4 font-semibold text-sky-400">
                      {span.agent_name}
                    </td>
                    <td className="py-2.5 pr-4 text-slate-200">
                      {span.event_type}
                    </td>
                    <td className="py-2.5 pr-4 text-indigo-300">{span.model}</td>
                    <td className="py-2.5 pr-4 text-slate-400">
                      {span.prompt_version}
                    </td>
                    <td className="py-2.5 pr-4 text-slate-300">
                      {span.tokens_in} / {span.tokens_out}
                    </td>
                    <td className="py-2.5 pr-4 text-emerald-400">
                      ${span.cost_usd.toFixed(6)}
                    </td>
                    <td className="py-2.5 pr-4 text-amber-300">
                      {span.latency_ms} ms
                    </td>
                    <td className="py-2.5">
                      <span className="rounded bg-emerald-500/20 px-2 py-0.5 text-emerald-300">
                        {span.status}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {activeTab === "economics" && (
        <section className="grid grid-cols-1 gap-6 md:grid-cols-2">
          <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-5">
            <h2 className="text-lg font-semibold text-white">
              Continuous Aggregate: `hourly_pr_costs` &amp; Budget Guard
            </h2>
            <p className="mt-1 text-xs text-slate-400">
              Real-time token spend circuit breaker trips automatically if 24h spend reaches DAILY_BUDGET_USD ($10.00).
            </p>
            <div className="mt-4 space-y-3 text-sm">
              <div className="flex justify-between border-b border-slate-800 pb-2">
                <span className="text-slate-400">Circuit Breaker State</span>
                <span className="font-mono font-semibold text-emerald-400">
                  CLOSED (Healthy)
                </span>
              </div>
              <div className="flex justify-between border-b border-slate-800 pb-2">
                <span className="text-slate-400">High-Reasoning Tier (Security / Quality)</span>
                <span className="font-mono text-slate-200">
                  claude-3-5-sonnet / gpt-4o ($0.018800)
                </span>
              </div>
              <div className="flex justify-between border-b border-slate-800 pb-2">
                <span className="text-slate-400">Fast Cost-Effective Tier (Testing / Docs)</span>
                <span className="font-mono text-slate-200">
                  claude-3-5-haiku / gpt-4o-mini ($0.002310)
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-slate-400">Semantic Embeddings (`vector(1536)`)</span>
                <span className="font-mono text-slate-200">
                  text-embedding-3-small ($0.000008)
                </span>
              </div>
            </div>
          </div>

          <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-5">
            <h2 className="text-lg font-semibold text-white">
              Continuous Aggregate: `agent_health_metrics`
            </h2>
            <p className="mt-1 text-xs text-slate-400">
              Per-specialist P95 latency, timeout isolation (NODE_TIMEOUT_SECONDS=30s), and selectivity rate.
            </p>
            <div className="mt-4 space-y-3 text-sm">
              <div className="flex justify-between border-b border-slate-800 pb-2">
                <span className="text-slate-400">Webhook Ingress Latency</span>
                <span className="font-mono text-emerald-400">14 ms (&lt; 200ms SLA)</span>
              </div>
              <div className="flex justify-between border-b border-slate-800 pb-2">
                <span className="text-slate-400">Parallel 4-Specialist Fan-Out Wall Time</span>
                <span className="font-mono text-sky-400">612 ms (Concurrent Send API)</span>
              </div>
              <div className="flex justify-between border-b border-slate-800 pb-2">
                <span className="text-slate-400">Grounded Evidence Rate (INV-7)</span>
                <span className="font-mono text-emerald-400">100.0%</span>
              </div>
              <div className="flex justify-between">
                <span className="text-slate-400">Clean PR Selectivity Score</span>
                <span className="font-mono text-emerald-400">100.0% (0 False Positives)</span>
              </div>
            </div>
          </div>
        </section>
      )}
    </div>
  );
}
