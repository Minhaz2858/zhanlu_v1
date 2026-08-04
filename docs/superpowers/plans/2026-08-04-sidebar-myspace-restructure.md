# Sidebar + My Space Restructure — Simplified Plan

> **Simplified scope (2026-08-04 2nd revision):** Only move "My Files" from inside My Space to the sidebar as a standalone top-level nav item. My Space stays as-is (Projects, Agents, KB tabs remain).

**Files:**

| Action | File |
|--------|------|
| Create | `frontend/src/pages/MyFilesPage.jsx` — page wrapping `FilesView` with header |
| Modify | `frontend/src/pages/MySpace.jsx` — remove "file" tab |
| Modify | `frontend/src/components/Sidebar.jsx` — add `/my-files` nav item |
| Modify | `frontend/src/App.jsx` — add `/my-files` route (desktop + mobile) |
| Modify | `frontend/src/lib/translations.js` — add `myFiles` sidebar key (zh+en) |
| Modify | `frontend/src/components/mobile/BottomTabBar.jsx` — add My Files tab |
| Create | `frontend/src/pages/mobile/MobileMyFilesPage.jsx` — mobile wrapper |

**No changes to:** `ProjectsView`, `AgentsView`, `FilesView`, backend services.

**Tech Stack:** React 18, React Router v6, Tailwind CSS, lucide-react icons, existing `base44.entities` SDK.

## Tasks

### Task 1: Add `myFiles` translation keys
`translations.js` — add `myFiles: '我的文件'` (zh) and `myFiles: 'My Files'` (en) in sidebar section.

### Task 2: Create `MyFilesPage.jsx`
Wrap `FilesView` with a PageHeader + "agent outputs" badge. Reuse existing FilesView component unchanged.

### Task 3: Remove "file" tab from `MySpace.jsx`
- Remove `file` from the tabs array
- Remove the `{activeTab === 'file' && <FilesView />}` render block
- Keep project, agent, kb tabs intact

### Task 4: Add `/my-files` routes in `App.jsx`
- Desktop: `<Route path="/my-files" element={<MyFilesPage />} />`
- Mobile: `<Route path="/my-files" element={<MobileMyFilesPage />} />`

### Task 5: Add "My Files" to sidebar nav
`Sidebar.jsx` — add `{ to: '/my-files', label: t.sidebar.myFiles, icon: Folder }` to navItems array. Keep all existing items.

### Task 6: Update mobile bottom tabs
`BottomTabBar.jsx` — add `/my-files` tab. Create mobile wrapper page.

### Task 7: Build verification
`npm run build` — verify no import errors, routes resolve.

---

**Backward compat:** `/my-space` unchanged. Deep links work as before. Only change is file tab moves from inside My Space to its own page.
