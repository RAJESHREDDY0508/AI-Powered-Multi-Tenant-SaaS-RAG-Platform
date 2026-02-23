'use client';

import { useState, useEffect, useCallback } from 'react';
import {
  Search, Filter, RefreshCw, FileText, Trash2,
  ExternalLink, ChevronLeft, ChevronRight,
} from 'lucide-react';
import { DocumentUpload }      from '@/components/features/DocumentUpload';
import { Card, CardHeader }    from '@/components/ui/Card';
import { Button }              from '@/components/ui/Button';
import { Input }               from '@/components/ui/Input';
import { Badge }               from '@/components/ui/Badge';
import { Spinner }             from '@/components/ui/Spinner';
import { useDocuments }        from '@/hooks/useDocuments';
import { useDocumentStore }    from '@/store/documentStore';
import { useToast }            from '@/hooks/useToast';
import type { Document, DocumentStatus } from '@/types/documents';

type BadgeVariant = 'success' | 'warning' | 'danger' | 'info' | 'default';

const STATUS_BADGE: Record<DocumentStatus, { label: string; variant: BadgeVariant }> = {
  pending:    { label: 'Pending',    variant: 'default' },
  processing: { label: 'Processing', variant: 'warning' },
  complete:   { label: 'Ready',      variant: 'success' },
  failed:     { label: 'Failed',     variant: 'danger'  },
};

function DocumentRow({
  doc,
  onDelete,
}: {
  doc: Document;
  onDelete: (id: string) => void;
}) {
  const badge = STATUS_BADGE[doc.status];
  return (
    <tr className="border-b border-gray-100 dark:border-gray-800 hover:bg-gray-50 dark:hover:bg-gray-800/50 transition-colors">
      <td className="py-3 px-4">
        <div className="flex items-center gap-2">
          <FileText className="h-4 w-4 text-gray-400 shrink-0" />
          <span className="text-sm font-medium text-gray-800 dark:text-gray-100 truncate max-w-xs">
            {doc.filename}
          </span>
        </div>
      </td>
      <td className="py-3 px-4 text-sm text-gray-500 dark:text-gray-400">
        {(doc.file_size_bytes / (1024 * 1024)).toFixed(2)} MB
      </td>
      <td className="py-3 px-4">
        <Badge variant={badge.variant}>{badge.label}</Badge>
      </td>
      <td className="py-3 px-4 text-sm text-gray-400 dark:text-gray-500">
        {doc.chunk_count != null ? `${doc.chunk_count} chunks` : '—'}
      </td>
      <td className="py-3 px-4 text-sm text-gray-400 dark:text-gray-500">
        {new Date(doc.created_at).toLocaleDateString()}
      </td>
      <td className="py-3 px-4">
        <button
          onClick={() => onDelete(doc.id)}
          className="p-1.5 text-gray-400 hover:text-red-500 rounded transition-colors"
          aria-label="Delete document"
        >
          <Trash2 className="h-4 w-4" />
        </button>
      </td>
    </tr>
  );
}

export default function DocumentsPage() {
  const { fetchDocuments, deleteDocument } = useDocuments();
  const documents = useDocumentStore((s) => s.documents);
  const { error: toastError, success } = useToast();

  const [search,  setSearch]  = useState('');
  const [loading, setLoading] = useState(true);
  const [page,    setPage]    = useState(1);
  const PAGE_SIZE = 20;

  const load = useCallback(async () => {
    setLoading(true);
    await fetchDocuments({ search: search || undefined, page, page_size: PAGE_SIZE });
    setLoading(false);
  }, [fetchDocuments, search, page]);

  useEffect(() => { load(); }, [load]);

  const handleDelete = async (id: string) => {
    if (!confirm('Delete this document? This action cannot be undone.')) return;
    try {
      await deleteDocument(id);
      success('Document deleted');
    } catch {
      toastError('Failed to delete document');
    }
  };

  const filtered = documents.filter((d) =>
    !search || d.filename.toLowerCase().includes(search.toLowerCase()),
  );

  return (
    <div className="space-y-6 max-w-6xl">
      {/* Upload section */}
      <Card>
        <CardHeader title="Upload Documents" subtitle="Drag & drop or click to upload PDF, DOCX, TXT, or Markdown files" />
        <DocumentUpload />
      </Card>

      {/* Document library */}
      <Card padding="none">
        {/* Toolbar */}
        <div className="flex items-center gap-3 p-4 border-b border-gray-100 dark:border-gray-800">
          <div className="flex-1">
            <Input
              placeholder="Search documents…"
              leftIcon={<Search className="h-4 w-4" />}
              value={search}
              onChange={(e) => { setSearch(e.target.value); setPage(1); }}
            />
          </div>
          <Button
            variant="ghost"
            size="sm"
            onClick={load}
            leftIcon={<RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />}
          >
            Refresh
          </Button>
        </div>

        {/* Table */}
        {loading ? (
          <div className="flex justify-center py-12">
            <Spinner size="lg" className="text-brand-500" />
          </div>
        ) : filtered.length === 0 ? (
          <div className="text-center py-12 text-gray-400">
            <FileText className="h-10 w-10 mx-auto mb-2 text-gray-300" />
            <p className="text-sm">No documents found</p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left">
              <thead>
                <tr className="text-xs uppercase text-gray-400 border-b border-gray-100 dark:border-gray-800">
                  <th className="py-2 px-4 font-medium">Name</th>
                  <th className="py-2 px-4 font-medium">Size</th>
                  <th className="py-2 px-4 font-medium">Status</th>
                  <th className="py-2 px-4 font-medium">Chunks</th>
                  <th className="py-2 px-4 font-medium">Uploaded</th>
                  <th className="py-2 px-4" />
                </tr>
              </thead>
              <tbody>
                {filtered.map((doc) => (
                  <DocumentRow key={doc.id} doc={doc} onDelete={handleDelete} />
                ))}
              </tbody>
            </table>
          </div>
        )}

        {/* Pagination */}
        <div className="flex items-center justify-between px-4 py-3 border-t border-gray-100 dark:border-gray-800">
          <p className="text-xs text-gray-400">{filtered.length} document{filtered.length !== 1 ? 's' : ''}</p>
          <div className="flex gap-1">
            <Button
              variant="ghost"
              size="xs"
              onClick={() => setPage((p) => Math.max(1, p - 1))}
              disabled={page === 1}
              leftIcon={<ChevronLeft className="h-3 w-3" />}
            >
              Prev
            </Button>
            <span className="flex items-center px-2 text-xs text-gray-500">Page {page}</span>
            <Button
              variant="ghost"
              size="xs"
              onClick={() => setPage((p) => p + 1)}
              disabled={filtered.length < PAGE_SIZE}
              rightIcon={<ChevronRight className="h-3 w-3" />}
            >
              Next
            </Button>
          </div>
        </div>
      </Card>
    </div>
  );
}
