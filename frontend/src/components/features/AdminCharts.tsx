'use client';

import {
  ResponsiveContainer,
  AreaChart,
  Area,
  BarChart,
  Bar,
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  CartesianGrid,
  Legend,
} from 'recharts';
import { Card, CardHeader } from '@/components/ui/Card';

/* ── Shared tooltip style ─────────────────────────────────── */
const tooltipStyle = {
  contentStyle: {
    background: '#1e293b',
    border:      'none',
    borderRadius: '8px',
    color:       '#f1f5f9',
    fontSize:    '12px',
  },
  cursor: { fill: 'rgba(99,102,241,0.08)' },
};

/* ── Token Usage Over Time (Area) ─────────────────────────── */
interface TokenUsagePoint {
  date:          string;
  input_tokens:  number;
  output_tokens: number;
}

export function TokenUsageChart({ data }: { data: TokenUsagePoint[] }) {
  return (
    <Card>
      <CardHeader
        title="Token Usage"
        subtitle="Daily input vs output tokens"
      />
      <ResponsiveContainer width="100%" height={220}>
        <AreaChart data={data} margin={{ top: 4, right: 4, left: -10, bottom: 0 }}>
          <defs>
            <linearGradient id="inputGrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%"  stopColor="#6366f1" stopOpacity={0.3} />
              <stop offset="95%" stopColor="#6366f1" stopOpacity={0}   />
            </linearGradient>
            <linearGradient id="outputGrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%"  stopColor="#10b981" stopOpacity={0.3} />
              <stop offset="95%" stopColor="#10b981" stopOpacity={0}   />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke="#334155" strokeOpacity={0.4} />
          <XAxis dataKey="date" tick={{ fontSize: 11, fill: '#94a3b8' }} tickLine={false} axisLine={false} />
          <YAxis tick={{ fontSize: 11, fill: '#94a3b8' }} tickLine={false} axisLine={false} />
          <Tooltip {...tooltipStyle} />
          <Legend wrapperStyle={{ fontSize: 12 }} />
          <Area type="monotone" dataKey="input_tokens"  name="Input"  stroke="#6366f1" fill="url(#inputGrad)"  strokeWidth={2} dot={false} />
          <Area type="monotone" dataKey="output_tokens" name="Output" stroke="#10b981" fill="url(#outputGrad)" strokeWidth={2} dot={false} />
        </AreaChart>
      </ResponsiveContainer>
    </Card>
  );
}

/* ── Cost by Model (Bar) ──────────────────────────────────── */
interface CostByModelPoint {
  model:    string;
  cost_usd: number;
}

export function CostByModelChart({ data }: { data: CostByModelPoint[] }) {
  return (
    <Card>
      <CardHeader
        title="Cost by Model"
        subtitle="USD spend this month"
      />
      <ResponsiveContainer width="100%" height={220}>
        <BarChart data={data} margin={{ top: 4, right: 4, left: -10, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#334155" strokeOpacity={0.4} vertical={false} />
          <XAxis dataKey="model" tick={{ fontSize: 11, fill: '#94a3b8' }} tickLine={false} axisLine={false} />
          <YAxis tick={{ fontSize: 11, fill: '#94a3b8' }} tickLine={false} axisLine={false} tickFormatter={(v) => `$${v.toFixed(2)}`} />
          <Tooltip
            {...tooltipStyle}
            formatter={(v: number) => [`$${v.toFixed(4)}`, 'Cost']}
          />
          <Bar dataKey="cost_usd" name="Cost (USD)" fill="#6366f1" radius={[4, 4, 0, 0]} maxBarSize={48} />
        </BarChart>
      </ResponsiveContainer>
    </Card>
  );
}

/* ── Latency p95 Over Time (Line) ─────────────────────────── */
interface LatencyPoint {
  date:       string;
  p50_ms:     number;
  p95_ms:     number;
}

export function LatencyChart({ data }: { data: LatencyPoint[] }) {
  return (
    <Card>
      <CardHeader
        title="Query Latency"
        subtitle="p50 / p95 in milliseconds"
      />
      <ResponsiveContainer width="100%" height={220}>
        <LineChart data={data} margin={{ top: 4, right: 4, left: -10, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#334155" strokeOpacity={0.4} />
          <XAxis dataKey="date" tick={{ fontSize: 11, fill: '#94a3b8' }} tickLine={false} axisLine={false} />
          <YAxis tick={{ fontSize: 11, fill: '#94a3b8' }} tickLine={false} axisLine={false} tickFormatter={(v) => `${v}ms`} />
          <Tooltip {...tooltipStyle} formatter={(v: number) => [`${v}ms`]} />
          <Legend wrapperStyle={{ fontSize: 12 }} />
          <Line type="monotone" dataKey="p50_ms" name="p50" stroke="#6366f1" strokeWidth={2} dot={false} />
          <Line type="monotone" dataKey="p95_ms" name="p95" stroke="#f59e0b" strokeWidth={2} dot={false} strokeDasharray="4 2" />
        </LineChart>
      </ResponsiveContainer>
    </Card>
  );
}

/* ── RAG Quality Scores (Bar grouped) ────────────────────── */
interface QualityPoint {
  date:              string;
  faithfulness:      number;
  answer_relevance:  number;
  context_precision: number;
}

export function QualityChart({ data }: { data: QualityPoint[] }) {
  return (
    <Card>
      <CardHeader
        title="RAG Quality Metrics"
        subtitle="RAGAS evaluation scores (0–1)"
      />
      <ResponsiveContainer width="100%" height={220}>
        <BarChart data={data} margin={{ top: 4, right: 4, left: -10, bottom: 0 }} barGap={2}>
          <CartesianGrid strokeDasharray="3 3" stroke="#334155" strokeOpacity={0.4} vertical={false} />
          <XAxis dataKey="date" tick={{ fontSize: 11, fill: '#94a3b8' }} tickLine={false} axisLine={false} />
          <YAxis domain={[0, 1]} tick={{ fontSize: 11, fill: '#94a3b8' }} tickLine={false} axisLine={false} tickFormatter={(v) => v.toFixed(1)} />
          <Tooltip {...tooltipStyle} formatter={(v: number) => [v.toFixed(3)]} />
          <Legend wrapperStyle={{ fontSize: 12 }} />
          <Bar dataKey="faithfulness"      name="Faithfulness"      fill="#6366f1" radius={[3,3,0,0]} maxBarSize={20} />
          <Bar dataKey="answer_relevance"  name="Answer Relevance"  fill="#10b981" radius={[3,3,0,0]} maxBarSize={20} />
          <Bar dataKey="context_precision" name="Context Precision" fill="#f59e0b" radius={[3,3,0,0]} maxBarSize={20} />
        </BarChart>
      </ResponsiveContainer>
    </Card>
  );
}
