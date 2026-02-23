/**
 * UI Store — Zustand
 * Theme, sidebar collapse, notification badges.
 */

import { create } from 'zustand';
import { persist } from 'zustand/middleware';

type Theme = 'light' | 'dark' | 'system';

interface UIStore {
  theme:           Theme;
  sidebarOpen:     boolean;
  sidebarCollapsed: boolean;

  setTheme:        (t: Theme) => void;
  toggleSidebar:   () => void;
  collapseSidebar: (v: boolean) => void;
}

export const useUIStore = create<UIStore>()(
  persist(
    (set) => ({
      theme:            'system',
      sidebarOpen:      true,
      sidebarCollapsed: false,

      setTheme:  (t) => set({ theme: t }),
      toggleSidebar:   () => set((s) => ({ sidebarOpen: !s.sidebarOpen })),
      collapseSidebar: (v) => set({ sidebarCollapsed: v }),
    }),
    {
      name:    'ui-preferences',
      partialize: (s) => ({ theme: s.theme, sidebarCollapsed: s.sidebarCollapsed }),
    }
  )
);
