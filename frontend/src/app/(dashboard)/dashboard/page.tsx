'use client';

import { useEffect, useState } from 'react';
import { useRouter }           from 'next/navigation';
import {
  FileText, MessageSquare, Zap, TrendingUp,
  AlertCircle, ArrowRight,
} from 'lucide-react';
import { Card, CardHeader }    from '@/components/ui/Card';
import { Button }              from '@/components/ui/Button';
import { PageSpinner }         from '@/components/ui/Spinner';
import { useAuthStore }        from '@/store/authStore';
import { ROUTES }              from '@/lib/constants';
import { apiGet }              from '@/lib/api';

interface OverviewStats {
  total_documents:         number;
  processing_count:        number;
  failed_count:            number;
  total_queries:           number;
  total_tokens_this_month: number;
  cost_usd_this_month:     number;
  avg_faithfulness:        number | null;
}

function StatCard({
  label, value, icon: Icon, sub, variant = 'default',
}: {
  label:    string;
  value:    string | number;
  icon:     React.ElementType;
  sub?:     string;
  variant?: 'default' | 'warning' | 'success';
}) {
  const colours = {
    default: 'text-brand-600',
    warning: 'text-amber-500',
    success: 'text-emerald-500',
  };
  return (
    <Card>
      <div className="flex items-start justify-between">
        <div>
          <p className="text-xs text-gray-500 dark:text-gray-400 uppercase tracking-wide font-medium">{label}</p>
          <p className="text-3xl font-bold text-gray-900 dark:text-gray-100 mt-1">{value}</p>
          {sub && <p className="text-xs text-gray-400 mt-1">{sub}</p>}
        </div>
        <Icon className={`h-8 w-8 ${colours[variant]}`} />
      </div>
    </Card>
  );
}

export default function OverviewPage() {
  const user   = useAuthStore((s) => s.user);
  const router = useRouter();
  const [stats,   setStats]   = useState<OverviewStats | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    apiGet<OverviewStats>('/api/v1/admin/overview')
      .then((data) => setStats(data))
      .catch(() => {/* swallow — show partial UI */})
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <PageSpinner />;

  return (
    <div className="space-y-6 max-w-6xl">
      {/* Welcome banner */}
      <div>
        <h2 className="text-2xl font-bold text-gray-900 dark:text-gray-100">
          Welcome back{user?.name ? `, ${user.name.split(' ')[0]}` : ''}! 👋
        </h2>
        <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
          {user?.tenantId} · {new Date().toLocaleDateString('en-US', { weekday: 'long', month: 'long', day: 'numeric' })}
        </p>
      </div>

      {/* Stats row */}
      <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-4 gap-4">
        <StatCard
          label="Documents"
          value={stats?.total_documents ?? '—'}
          icon={FileText}
          sub={stats?.processing_count ? `${stats.processing_count} processing` : undefined}
        />
        <StatCard
          label="Total Queries"
          value={stats?.total_queries ?? '—'}
          icon={MessageSquare}
        />
        <StatCard
          label="Tokens This Month"
          value={stats?.total_tokens_this_month?.toLocaleString() ?? '—'}
          icon={Zap}
          sub={stats?.cost_usd_this_month != null ? `$${stats.cost_usd_this_month.toFixed(2)} USD` : undefined}
          variant="success"
        />
        <StatCard
          label="Avg Faithfulness"
          value={stats?.avg_faithfulness != null ? (stats.avg_faithfulness * 100).toFixed(0) + '%' : '—'}
          icon={TrendingUp}
          variant="success"
        />
      </div>

      {/* Warnings */}
      {(stats?.failed_count ?? 0) > 0 && (
        <div className="flex items-center gap-3 rounded-xl border border-amber-200 bg-amber-50 dark:border-amber-800 dark:bg-amber-900/20 p-4">
          <AlertCircle className="h-5 w-5 text-amber-500 shrink-0" />
          <p className="text-sm text-amber-700 dark:text-amber-300">
            {stats!.failed_count} document{stats!.failed_count > 1 ? 's' : ''} failed ingestion.{' '}
            <button
              onClick={() => router.push(ROUTES.DOCUMENTS)}
              className="font-semibold underline"
            >
              Review in Documents
            </button>
          </p>
        </div>
      )}

      {/* Quick actions */}
      <div className="grid sm:grid-cols-2 gap-4">
        <Card hover onClick={() => router.push(ROUTES.DOCUMENTS)}>
          <CardHeader title="Upload Documents" subtitle="Add new knowledge to your workspace" />
          <Button variant="outline" size="sm" rightIcon={<ArrowRight className="h-4 w-4" />}>
            Go to Documents
          </Button>
        </Card>
        <Card hover onClick={() => router.push(ROUTES.CHAT)}>
          <CardHeader title="Ask the AI" subtitle="Chat with your documents instantly" />
          <Button variant="outline" size="sm" rightIcon={<ArrowRight className="h-4 w-4" />}>
            Open Chat
          </Button>
        </Card>
      </div>
    </div>
  );
}
