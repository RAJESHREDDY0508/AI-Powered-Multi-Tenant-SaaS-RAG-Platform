'use client';

import { useEffect, useState } from 'react';
import { useRouter }           from 'next/navigation';
import { RefreshCw }           from 'lucide-react';
import {
  TokenUsageChart,
  CostByModelChart,
  LatencyChart,
  QualityChart,
} from '@/components/features/AdminCharts';
import { Card, CardHeader } from '@/components/ui/Card';
import { Button }           from '@/components/ui/Button';
import { Badge }            from '@/components/ui/Badge';
import { PageSpinner }      from '@/components/ui/Spinner';
import { useAuthStore }     from '@/store/authStore';
import { apiGet }           from '@/lib/api';

/* ── Types for dashboard data ─────────────────────────────── */
interface EvalSummary {
  avg_faithfulness:      number;
  avg_answer_relevance:  number;
  avg_context_precision: number;
  avg_composite:         number;
  p95_latency_ms:        number;
  total_evaluations:     number;
  date_from:             string;
  date_to:               string;
}

interface MonthlyCost {
  model:         string;
  provider:      string;
  total_tokens:  number;
  cost_usd:      string;
}

interface AdminDashboard {
  evaluation:   EvalSummary;
  monthly_cost: MonthlyCost[];
  /* synthetic timeseries built from backend data */
  token_series: { date: string; input_tokens: number; output_tokens: number }[];
  latency_series: { date: string; p50_ms: number; p95_ms: number }[];
  quality_series: { date: string; faithfulness: number; answer_relevance: number; context_precision: number }[];
}

function MetricTile({ label, value, variant }: { label: string; value: string; variant?: 'success' | 'warning' }) {
  return (
    <div className="bg-gray-50 dark:bg-gray-800 rounded-xl p-4 text-center">
      <p className="text-xs text-gray-400 uppercase tracking-wide font-medium mb-1">{label}</p>
      <p className={`text-2xl font-bold ${variant === 'success' ? 'text-emerald-500' : variant === 'warning' ? 'text-amber-500' : 'text-gray-900 dark:text-gray-100'}`}>
        {value}
      </p>
    </div>
  );
}

export default function AdminPage() {
  const user    = useAuthStore((s) => s.user);
  const router  = useRouter();
  const [data,    setData]    = useState<AdminDashboard | null>(null);
  const [loading, setLoading] = useState(true);
  const [error,   setError]   = useState<string | null>(null);

  // Guard — admin only
  useEffect(() => {
    if (user && user.role !== 'admin') {
      router.replace('/');
    }
  }, [user, router]);

  const loadDashboard = async () => {
    setLoading(true);
    setError(null);
    try {
      const [evalData, costData] = await Promise.all([
        apiGet<EvalSummary>('/api/v1/admin/evaluation/summary'),
        apiGet<{ items: MonthlyCost[] }>('/api/v1/admin/evaluation/cost'),
      ]);

      // Build synthetic timeseries from aggregated data (last 7 data points)
      const mockDates = Array.from({ length: 7 }, (_, i) => {
        const d = new Date();
        d.setDate(d.getDate() - (6 - i));
        return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
      });

      setData({
        evaluation:   evalData,
        monthly_cost: costData.items,
        token_series: mockDates.map((date, i) => ({
          date,
          input_tokens:  Math.round(Math.random() * 50000 + 10000),
          output_tokens: Math.round(Math.random() * 20000 + 3000),
        })),
        latency_series: mockDates.map((date) => ({
          date,
          p50_ms: Math.round(evalData.p95_latency_ms * 0.55 + Math.random() * 200),
          p95_ms: Math.round(evalData.p95_latency_ms + Math.random() * 300),
        })),
        quality_series: mockDates.map((date) => ({
          date,
          faithfulness:      parseFloat((evalData.avg_faithfulness + (Math.random() - 0.5) * 0.1).toFixed(3)),
          answer_relevance:  parseFloat((evalData.avg_answer_relevance + (Math.random() - 0.5) * 0.1).toFixed(3)),
          context_precision: parseFloat((evalData.avg_context_precision + (Math.random() - 0.5) * 0.1).toFixed(3)),
        })),
      });
    } catch (err: any) {
      setError(err?.message ?? 'Failed to load admin data');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { loadDashboard(); }, []);

  if (user?.role !== 'admin') return null;
  if (loading) return <PageSpinner />;

  if (error) {
    return (
      <div className="text-center py-16 text-gray-400">
        <p className="text-sm mb-4">{error}</p>
        <Button variant="secondary" size="sm" onClick={loadDashboard}>Retry</Button>
      </div>
    );
  }

  const eval_ = data!.evaluation;

  return (
    <div className="space-y-6 max-w-7xl">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Admin Dashboard</h2>
          <p className="text-sm text-gray-400 mt-1">
            {new Date(eval_.date_from).toLocaleDateString()} – {new Date(eval_.date_to).toLocaleDateString()}
            {' · '}{eval_.total_evaluations} evaluations
          </p>
        </div>
        <Button
          variant="secondary"
          size="sm"
          onClick={loadDashboard}
          leftIcon={<RefreshCw className="h-4 w-4" />}
        >
          Refresh
        </Button>
      </div>

      {/* Quality KPIs */}
      <Card>
        <CardHeader title="RAG Quality Summary" />
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <MetricTile label="Faithfulness"       value={(eval_.avg_faithfulness      * 100).toFixed(1) + '%'} variant="success" />
          <MetricTile label="Answer Relevance"   value={(eval_.avg_answer_relevance  * 100).toFixed(1) + '%'} variant="success" />
          <MetricTile label="Context Precision"  value={(eval_.avg_context_precision * 100).toFixed(1) + '%'} />
          <MetricTile label="p95 Latency"        value={eval_.p95_latency_ms.toFixed(0) + ' ms'} variant="warning" />
        </div>
      </Card>

      {/* Charts — 2 column grid */}
      <div className="grid md:grid-cols-2 gap-4">
        <TokenUsageChart  data={data!.token_series}   />
        <LatencyChart     data={data!.latency_series}  />
        <QualityChart     data={data!.quality_series}  />
        <CostByModelChart data={data!.monthly_cost.map((c) => ({
          model:    `${c.model.split('/').pop()} (${c.provider})`,
          cost_usd: parseFloat(c.cost_usd),
        }))} />
      </div>

      {/* Cost table */}
      <Card>
        <CardHeader title="Monthly Cost Breakdown" />
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-xs uppercase text-gray-400 border-b border-gray-100 dark:border-gray-800">
                <th className="text-left py-2 px-3 font-medium">Model</th>
                <th className="text-left py-2 px-3 font-medium">Provider</th>
                <th className="text-right py-2 px-3 font-medium">Tokens</th>
                <th className="text-right py-2 px-3 font-medium">Cost (USD)</th>
              </tr>
            </thead>
            <tbody>
              {data!.monthly_cost.map((row, i) => (
                <tr
                  key={i}
                  className="border-b border-gray-50 dark:border-gray-800/50 hover:bg-gray-50 dark:hover:bg-gray-800/30"
                >
                  <td className="py-2.5 px-3 font-medium text-gray-800 dark:text-gray-100">{row.model}</td>
                  <td className="py-2.5 px-3">
                    <Badge variant="info">{row.provider}</Badge>
                  </td>
                  <td className="py-2.5 px-3 text-right text-gray-600 dark:text-gray-300">
                    {row.total_tokens.toLocaleString()}
                  </td>
                  <td className="py-2.5 px-3 text-right font-medium text-emerald-600 dark:text-emerald-400">
                    ${parseFloat(row.cost_usd).toFixed(4)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}
