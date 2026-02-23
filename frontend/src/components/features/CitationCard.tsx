'use client';

import { useState } from 'react';
import { clsx }     from 'clsx';
import { ChevronDown, ChevronUp, FileText, ExternalLink } from 'lucide-react';
import type { Citation } from '@/types/chat';

interface CitationCardProps {
  citation:  Citation;
  index:     number;
}

export function CitationCard({ citation, index }: CitationCardProps) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div
      className={clsx(
        'rounded-lg border text-sm transition-all duration-200',
        'border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-900',
      )}
    >
      {/* Header */}
      <button
        onClick={() => setExpanded((e) => !e)}
        className="flex items-start gap-2.5 w-full p-3 text-left"
        aria-expanded={expanded}
      >
        {/* Index badge */}
        <span className="mt-0.5 flex h-5 w-5 items-center justify-center rounded-full bg-brand-100 text-brand-700 dark:bg-brand-900/40 dark:text-brand-300 text-xs font-bold shrink-0">
          {index + 1}
        </span>

        <div className="flex-1 min-w-0">
          {/* Filename */}
          <div className="flex items-center gap-1.5 min-w-0">
            <FileText className="h-3.5 w-3.5 text-gray-400 shrink-0" />
            <span className="font-medium text-gray-800 dark:text-gray-100 truncate">
              {citation.document_name ?? 'Document'}
            </span>
          </div>

          {/* Score bar */}
          {citation.score !== undefined && (
            <div className="flex items-center gap-2 mt-1">
              <div className="flex-1 h-1 rounded-full bg-gray-200 dark:bg-gray-700">
                <div
                  className="h-1 rounded-full bg-brand-500"
                  style={{ width: `${Math.round(citation.score * 100)}%` }}
                />
              </div>
              <span className="text-xs text-gray-400 shrink-0">
                {Math.round(citation.score * 100)}%
              </span>
            </div>
          )}

          {/* Chunk location */}
          {citation.page_number !== undefined && (
            <p className="text-xs text-gray-400 mt-0.5">
              Page {citation.page_number}
              {citation.chunk_index !== undefined && ` · Chunk ${citation.chunk_index}`}
            </p>
          )}
        </div>

        {/* Expand toggle */}
        <span className="shrink-0 text-gray-400">
          {expanded ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
        </span>
      </button>

      {/* Expanded content */}
      {expanded && citation.content && (
        <div className="px-3 pb-3 border-t border-gray-100 dark:border-gray-800">
          <blockquote className="mt-2 text-xs leading-relaxed text-gray-600 dark:text-gray-300 bg-gray-50 dark:bg-gray-800 rounded p-2.5 italic border-l-2 border-brand-400">
            {citation.content.slice(0, 600)}
            {citation.content.length > 600 && '…'}
          </blockquote>
        </div>
      )}
    </div>
  );
}

/** Collapsible list of citations shown beneath a chat message */
export function CitationList({ citations }: { citations: Citation[] }) {
  const [open, setOpen] = useState(false);

  if (!citations.length) return null;

  return (
    <div className="mt-3">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex items-center gap-1.5 text-xs font-medium text-brand-600 dark:text-brand-400 hover:underline"
      >
        <FileText className="h-3.5 w-3.5" />
        {citations.length} source{citations.length !== 1 ? 's' : ''}
        {open ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
      </button>

      {open && (
        <div className="mt-2 space-y-2 animate-fade-in">
          {citations.map((c, i) => (
            <CitationCard key={c.chunk_id ?? i} citation={c} index={i} />
          ))}
        </div>
      )}
    </div>
  );
}
