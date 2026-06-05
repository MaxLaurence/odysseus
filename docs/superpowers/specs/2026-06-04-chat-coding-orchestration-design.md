# Chat Coding Orchestration Design

Date: 2026-06-04

## Goal

Make one chat able to orchestrate many Coding Station agents without forcing the
user to leave the chat surface. Code Station remains the full terminal/workspace
drill-down, but the chat becomes the command center for launching, monitoring,
steering, reviewing, and verifying concurrent coding runs.

## Scope Model

Each chat has an optional coding scope. The scope controls which background runs
appear in the chat run board.

Scope types:

- `personal`: no project scope. Show runs launched by this chat by default.
- `project`: one Coding Station project. Show all active owner runs for that
  project, plus every run launched by this chat.
- `composite`: a named set of Coding Station projects. Show all active owner
  runs for any project in the set, grouped by project, plus every run launched by
  this chat.

Chat-launched runs are always pinned into that chat board even if they no longer
match the current scope. This preserves the user's orchestration context when a
scope is edited, narrowed, or replaced.

## Composite Scopes

Composite scopes are Odysseus-suggested and user-owned.

Odysseus may auto-create a chat-local composite when a chat launches agents in
more than one project. It may suggest a composite when the conversation references
multiple projects before launch, but it should not silently broaden the run board
until there is a concrete coding action.

Composite metadata should preserve provenance:

- `pinned_project_ids`: user explicitly kept these projects.
- `suggested_project_ids`: Odysseus inferred these projects.
- `ignored_project_ids`: user explicitly removed these projects.
- `source`: `auto`, `manual`, or `mixed`.
- `reason`: short explanation for why the composite exists.

Auto-created composites start chat-local. Users can rename, edit, remove, or
promote them into reusable named composites. Once a user edits an auto composite,
it becomes `mixed`; ignored projects should not be repeatedly re-added by
automation.

## Run Board Contract

The chat run board owns the shared card contract for all chat-side coding
features. Each card is keyed by `run_id` and includes:

- `run_id`, `thread_id`, `project_id`, optional `issue_id`, and originating
  `session_id`.
- status, harness, title, project name, and scope membership reason.
- stream state: collapsed, expanded, attached, detached, reconnecting, terminal.
- last seen event sequence for `after_seq` resume.
- bounded live output tail with unread count while collapsed.
- actions: open in Code Station, stop, send stdin, expand stream, collapse stream.
- later extensions: artifact cards, verification state, approval/apply actions.

Cards subscribe to per-run SSE streams only when expanded, focused, or within the
active render cap. Collapsed cards stay tracked through lightweight status
refreshes and unread counts. Closing or collapsing one card must not affect any
other run.

## Context Packet

When chat spawns a coding thread, it should pass a structured context packet, not
just a task string.

The packet should include:

- task summary and exact user ask.
- why this work matters in the current conversation.
- success criteria and any explicit validation constraints.
- relevant chat excerpts.
- linked `session_id`, coding scope id, project ids, and optional Bead id.
- referenced docs, notes, memory entries, or files when selected by the chat.

The packet should be persisted with the thread/run metadata and made available to
the coding harness as launch context. The packet is not a replacement for Beads:
project work items still belong in Beads, and durable user/project facts still
belong in memory.

## Backend Seams

Use existing Code Station primitives where possible:

- `routes/coding_routes.py` remains the HTTP/SSE API surface.
- `src/coding_runtime.py` remains the run/event/runtime service.
- `src/tool_implementations.py::do_manage_coding` remains the chat agent facade.
- `src/coding_followup.py` remains the one-shot finished-run wake-up path.

New durable state should live in the database layer, not browser-only state:

- chat to coding scope association.
- reusable composite scope records.
- chat-launched run links for "always pinned" behavior.
- context packet metadata.

The run board discovery query should combine:

- active owner runs matching the chat coding scope.
- active or recently finished runs launched by this chat.
- enough project/thread metadata to group and label cards without leaking
  filesystem internals.

## Run Board Discovery API

The chat frontend should not infer run-board membership from `/api/coding/queue`.
That endpoint can remain the Code Station/global queue view. Chat orchestration
needs an explicit discovery contract that resolves the current chat scope,
chat-launched pinned runs, grouping, and authorization in one backend-owned
place.

Preferred endpoint shape:

- `GET /api/coding/chat/{session_id}/run-board`
- Optional query parameters:
  - `include_finished`: include recently terminal chat-launched runs, default
    `true`.
  - `finished_window`: bounded lookback such as `24h`, default controlled by the
    backend.
  - `include_owner_activity`: only valid for personal scope when the user expands
    beyond chat-launched runs.

The response should include:

- `scope`: resolved scope id, type, name, source, project ids, and whether it is
  chat-local or reusable.
- `groups`: project-grouped buckets for project/composite scopes.
- `cards`: normalized run-card records using the shared run-board contract.
- `counts`: active, queued, terminal-recent, attached-cap, and total tracked.
- `policy`: active stream cap and any reason the board is degraded.

Each card should include a `membership_reason` such as `scope_project`,
`scope_composite`, `chat_launched`, or `chat_launched_out_of_scope`. If a run
matches both scope and chat launch, `chat_launched` should win for pinning and
retention semantics.

Authorization and sanitization stay server-side. The endpoint must only return
runs owned by the current user and authorized for the session/scope. It must not
return filesystem internals such as `run_dir`, `tmux_session`, or `log_path`.

The first implementation slice should ship this endpoint as read-only discovery:
scope resolution plus cards/counts, without live SSE attachment, stdin/stop
controls, artifacts, or verification.

## UX Behavior

The first visible surface is a compact run rail or board in chat:

- Project scope: show that project's active agents.
- Composite scope: show active agents grouped by project.
- Personal scope: show this chat's launched agents, with an affordance to expand
  to broader owner activity.

The scope chip near the chat title should show the current scope and open a small
manager for rename, add/remove projects, promote chat-local composite, or switch
scope. Code Station remains the "open full workspace" action for terminals,
split panes, Beads board, and deep inspection.

## Ordering

1. Add chat coding scope model.
2. Add explicit run-board discovery API.
3. Add chat run-card contract and static scoped board/rail rendering.
4. Add per-run lazy streaming.
5. Add structured context handoff for chat-spawned threads.
6. Add chat card controls: stop and stdin.
7. Add artifact and verification extensions.
8. Add docked split-pane as a secondary workflow improvement.

## First Implementation Slice

The first slice should prove the backend semantics before any live streaming UI:

- durable chat coding scope records for `personal`, `project`, and `composite`.
- chat-local auto composite creation after concrete multi-project coding
  launches.
- manual scope management primitives needed by the backend/API, even if the first
  frontend is minimal.
- chat-launched run links for always-pinned board membership.
- `GET /api/coding/chat/{session_id}/run-board` returning resolved scope,
  groups, cards, counts, and policy.
- focused backend tests for project scope, composite scope, personal scope,
  out-of-scope pinned chat-launched runs, owner/session authorization, and
  sanitized card payloads.

Explicitly out of the first slice:

- live SSE rendering inside chat cards.
- stdin/stop controls from chat.
- artifact/diff cards.
- verification loop.
- docked split-pane.
- polished scope manager UI beyond a small debug/basic surface if needed to
  exercise the backend.

## Verification

Focused tests should cover:

- project scope shows active owner runs for that project.
- composite scope shows active owner runs for all member projects.
- chat-launched runs remain visible after scope changes.
- run-board discovery endpoint returns scoped plus pinned runs without frontend
  inference from `/api/coding/queue`.
- auto composite creation on multi-project chat launches.
- ignored projects are not re-added automatically.
- per-run streaming resumes with `after_seq` and does not cross streams.
- collapsed cards close SSE connections and keep lightweight status only.
- finished-run follow-up still fires exactly once.
