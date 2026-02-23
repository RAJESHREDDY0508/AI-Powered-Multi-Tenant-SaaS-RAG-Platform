'use client';

import { useEffect, useState } from 'react';
import { Plus, MessageSquare, Trash2 } from 'lucide-react';
import { clsx }             from 'clsx';
import { ChatWindow }       from '@/components/features/ChatWindow';
import { Button }           from '@/components/ui/Button';
import { useChatStore }     from '@/store/chatStore';

export default function ChatPage() {
  const sessions       = useChatStore((s) => Object.values(s.sessions));
  const createSession  = useChatStore((s) => s.createSession);
  const deleteSession  = useChatStore((s) => s.deleteSession);

  const [activeId, setActiveId] = useState<string | null>(null);

  // Auto-create a session on first load
  useEffect(() => {
    if (sessions.length === 0) {
      const id = createSession();
      setActiveId(id);
    } else if (!activeId || !sessions.find((s) => s.id === activeId)) {
      setActiveId(sessions[0].id);
    }
  }, [sessions, activeId, createSession]);

  const handleNew = () => {
    const id = createSession();
    setActiveId(id);
  };

  const handleDelete = (id: string) => {
    deleteSession(id);
    if (activeId === id) {
      const remaining = sessions.filter((s) => s.id !== id);
      setActiveId(remaining[0]?.id ?? null);
    }
  };

  return (
    <div className="flex h-[calc(100vh-7rem)] gap-4">
      {/* Session list (sidebar) */}
      <div className="hidden md:flex flex-col w-56 shrink-0 gap-1">
        <Button
          size="sm"
          fullWidth
          variant="secondary"
          onClick={handleNew}
          leftIcon={<Plus className="h-4 w-4" />}
        >
          New chat
        </Button>

        <div className="flex-1 overflow-y-auto mt-2 space-y-0.5">
          {sessions.map((session) => (
            <div
              key={session.id}
              className={clsx(
                'group flex items-center gap-2 rounded-lg px-3 py-2 cursor-pointer',
                'text-sm transition-colors',
                session.id === activeId
                  ? 'bg-brand-50 text-brand-700 dark:bg-brand-900/30 dark:text-brand-300'
                  : 'text-gray-600 hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-800',
              )}
              onClick={() => setActiveId(session.id)}
            >
              <MessageSquare className="h-4 w-4 shrink-0 text-gray-400" />
              <span className="flex-1 truncate">
                {session.title ?? 'New conversation'}
              </span>
              <button
                onClick={(e) => { e.stopPropagation(); handleDelete(session.id); }}
                className="invisible group-hover:visible p-0.5 text-gray-400 hover:text-red-500"
                aria-label="Delete session"
              >
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            </div>
          ))}
        </div>
      </div>

      {/* Chat window */}
      <div className="flex-1 min-w-0">
        {activeId ? (
          <ChatWindow sessionId={activeId} />
        ) : (
          <div className="flex items-center justify-center h-full text-gray-400">
            <div className="text-center">
              <MessageSquare className="h-12 w-12 mx-auto mb-2 text-gray-300" />
              <p className="text-sm">Select a conversation or start a new one</p>
              <Button className="mt-4" onClick={handleNew} size="sm">
                New chat
              </Button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
