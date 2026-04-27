# Three-Layer Recall for Hermes Implementation Plan

> For Hermes: use subagent-driven-development skill to implement this plan task-by-task.

Goal: turn Hermes' existing memory + session recall into a clean three-layer recall stack by restoring vault search, adding native knowledge tools, and expanding session recall with browse/read primitives.

Architecture: keep the current built-in curated memory as layer 1, keep SQLite-backed session recall as layer 2, and make layer 3 a first-class native knowledge-search tool backed by a local vault index. Do not replace built-in memory with another semantic memory backend yet. The highest-leverage, lowest-conflict path is to strengthen knowledge and session retrieval while keeping the injected memory layer small and curated.

Tech Stack: Python, Hermes tool registry, SQLite/FTS5 session store, QMD-backed Obsidian indexing, pytest.

---

## Recommendation

Choose this path:

1. Restore and harden QMD after rebuilds.
2. Add native `knowledge_search` and `knowledge_read` tools.
3. Add native `session_list` and `session_read` tools next to `session_search`.
4. Keep built-in curated memory as-is.
5. Delay any semantic memory-provider migration until the three-layer stack is stable.

Why this is the best fit here:
- It preserves the fork's upstream shape better than introducing a heavy new memory backend.
- It improves the layers currently missing or weak instead of over-investing in inline memory.
- It reuses systems already present in this repo: built-in memory, session DB, vault workflow, tool registry, and skills.
- It gives the agent a cleaner answer to three different questions:
  - memory → what does Umbra know about the user?
  - sessions → what have we already done?
  - knowledge → what is already written in the vault/docs?

Non-goal:
- Do not build a new generalized vector-memory subsystem for user facts.
- Do not replace `session_search` with embeddings in v1.
- Do not make knowledge retrieval part of the context engine. This is a recall/tooling problem, not a compression problem.

---

## Task 1: Restore QMD as a durable post-rebuild dependency

Objective: make the knowledge layer real again on DarkServer and stop losing it after rebuilds.

Files:
- Modify: `/app/code/Dockerfile.darkserver`
- Modify: `/app/code/start.sh` or the actual startup script used by the image
- Modify: `/root/.hermes/obsidian-vault/AI/Memory/Skills.md` only if commands/paths change
- Test: manual container verification

Step 1: Inspect current install/startup logic
- Confirm where global npm installs happen in `Dockerfile.darkserver`
- Confirm whether startup already checks QMD collection/index state
- Confirm the persistent QMD data dir strategy still exists or needs restoring

Step 2: Re-add QMD install if missing
- Install `@tobilu/qmd` in the image build.
- Keep the CPU-only patch described in the obsidian-search skill if the upstream package still tries GPU/Vulkan auto-builds.
- Ensure the patched file path is verified during build, not assumed.

Step 3: Persist index data outside ephemeral layers
- Keep QMD state under persistent Hermes storage, not container-local transient paths.
- If not already present, set a persistent data dir in the image/runtime env.

Step 4: Add startup verification
- On container startup:
  - if `qmd` binary is missing, log a clear warning
  - if collection is missing, create it
  - if index is missing, trigger background embed
- Avoid blocking startup on a full re-embed.

Step 5: Verify manually
Run:
- `command -v qmd`
- `qmd collection list`
- a small `qmd search ...` query
Expected:
- binary exists
- obsidian collection exists
- search returns results or at minimum a valid empty result, not a missing-binary failure

Step 6: Commit
```bash
git add Dockerfile.darkserver start.sh
git commit -m "fix: restore durable qmd knowledge index after rebuilds"
```

---

## Task 2: Add a small knowledge backend abstraction

Objective: avoid hard-coding QMD shell calls directly into tool logic.

Files:
- Create: `/app/code/agent/knowledge_backend.py`
- Test: `/app/code/tests/agent/test_knowledge_backend.py`

Step 1: Create the interface
Define a tiny backend abstraction with methods like:
- `is_available()`
- `search(query: str, limit: int = 5) -> dict`
- `read(ref: str) -> dict`
- `status() -> dict`

Step 2: Add QMD implementation
Implement a `QmdKnowledgeBackend` that:
- checks `qmd` availability
- shells out safely to `qmd search`, `qmd vsearch`, or `qmd query`
- parses stdout into structured JSON-friendly results
- supports `qmd get` for full-document reads when given a qmd URI

Step 3: Keep it strict and small
- No background indexing in the backend class
- No write operations
- No vault mutation logic
- Return structured failures when QMD is absent

Step 4: Add unit tests
Cover:
- binary missing
- successful search parse
- malformed output handling
- read/get success
- safe error payloads

Step 5: Commit
```bash
git add agent/knowledge_backend.py tests/agent/test_knowledge_backend.py
git commit -m "feat: add knowledge backend abstraction for vault recall"
```

---

## Task 3: Add native `knowledge_search` and `knowledge_read` tools

Objective: make the vault knowledge layer first-class instead of skill-only.

Files:
- Create: `/app/code/tools/knowledge_search_tool.py`
- Modify: `/app/code/toolsets.py`
- Modify: `/app/code/model_tools.py` only if explicit import/discovery wiring is needed
- Test: `/app/code/tests/tools/test_knowledge_search_tool.py`
- Docs: `/app/code/website/docs/reference/tools-reference` or the equivalent tool docs file if present

Step 1: Add `knowledge_search` schema
Suggested parameters:
- `query: string` required
- `limit: integer` optional, default 5, max 10
- `mode: string` optional: `keyword|semantic|hybrid`, default `hybrid`

Behavior:
- when QMD is available, execute the corresponding search
- when unavailable, return a grounded error suggesting rebuild/install recovery
- do not silently web-search as fallback; this tool is for local knowledge

Step 2: Add `knowledge_read` schema
Suggested parameters:
- `ref: string` required

Behavior:
- accepts a QMD URI or backend result identifier
- returns the full note/document text or a safe excerpt if very large

Step 3: Register tools in the right toolset
Best option:
- create a dedicated `knowledge` toolset if that is clean in this repo
Fallback option:
- include in the `file` or `session_search` adjacent area only if adding a new toolset is noisy

Preferred choice here: add a dedicated `knowledge` toolset. It is conceptually cleaner and matches the three-layer model.

Step 4: Add tests
Cover:
- tool unavailable when backend missing
- search success path
- read success path
- limit clamping
- mode validation
- safe JSON output shape

Step 5: Commit
```bash
git add tools/knowledge_search_tool.py toolsets.py tests/tools/test_knowledge_search_tool.py
git commit -m "feat: add native knowledge recall tools"
```

---

## Task 4: Split session recall into search, list, and read

Objective: make session recall match the knowledge layer in ergonomics and reduce overloading `session_search`.

Files:
- Modify: `/app/code/tools/session_search_tool.py`
- Test: `/app/code/tests/tools/test_session_search.py`
- Docs: relevant tools/session docs under `/app/code/website/docs/`

Step 1: Add `session_list`
Parameters:
- `limit` optional
- `source` optional

Behavior:
- returns recent sessions with id, title, source, start time, preview
- should not call the auxiliary model

Step 2: Add `session_read`
Parameters:
- `session_id` required
- `max_chars` optional

Behavior:
- returns raw or lightly formatted transcript excerpt for a single session
- should not summarize unless a future `summarize=true` option is added

Step 3: Keep `session_search` focused
- retain current keyword/FTS5 search + targeted summary flow
- update the description so `session_search` is no longer pretending to be list/read as well

Step 4: Add tests
Cover:
- recent listing
- source filtering
- reading missing session id
- reading long transcript truncation
- no-LMM recent/list/read paths

Step 5: Commit
```bash
git add tools/session_search_tool.py tests/tools/test_session_search.py
git commit -m "feat: expand session recall with list and read tools"
```

---

## Task 5: Teach the system prompt to use the right layer

Objective: reduce misuse of inline memory and guide the model toward the right retrieval tool.

Files:
- Modify: the Umbra system prompt source if it lives in config/prompt assembly
- Modify: `/app/code/agent/prompt_builder.py` only if the rule belongs in base Hermes behavior
- Test: prompt assembly tests if present

Step 1: Add concise routing guidance
Add a short rule block:
- user preferences/corrections/stable environment facts → `memory`
- past conversations/previous fixes → `session_search`, `session_list`, `session_read`
- vault notes/research/personal docs → `knowledge_search`, `knowledge_read`

Step 2: Keep it short
Do not bloat the prompt with a long manifesto. One compact routing section is enough.

Step 3: Verify no duplicate instruction conflicts
Check existing prompt text for overlap with `session_search` and memory instructions.

Step 4: Commit
```bash
git add agent/prompt_builder.py
git commit -m "docs: route recall tasks to memory session and knowledge layers"
```

---

## Task 6: Add recovery-focused documentation

Objective: make the three-layer system understandable and maintainable after the next rebuild.

Files:
- Modify: `/app/code/website/docs/user-guide/features/memory-providers.md`
- Modify: `/app/code/website/docs/user-guide/sessions.md`
- Create or modify: a tools/knowledge docs page in `/app/code/website/docs/user-guide/features/`
- Modify: `/root/.hermes/obsidian-vault/AI/Memory/Infrastructure.md` if operational commands change materially

Step 1: Document the three layers
Document:
- Memory = small curated facts injected at session start
- Sessions = SQLite transcript recall tools
- Knowledge = local vault/doc retrieval through QMD-backed tools

Step 2: Document failure modes
Include:
- `qmd` missing after rebuild
- collection/index missing
- session DB present but no search hits
- why knowledge search is local-only and not a web fallback

Step 3: Document operator recovery steps
Include exact commands for checking:
- binary
- collection list
- embed/reindex state

Step 4: Commit
```bash
git add website/docs/
git commit -m "docs: document three-layer recall architecture"
```

---

## Task 7: Optional v2 improvements after the core stack is stable

Objective: note follow-on work without polluting v1.

Files:
- No code in v1

Candidates:
- Hybrid session retrieval: FTS5 candidate generation + embedding rerank
- Recall broker tool that dispatches between memory/session/knowledge layers
- Structured search results with citations/snippets across all recall tools
- Non-Obsidian knowledge sources beyond the vault
- Native startup self-healing around QMD index freshness

Do not start these until Tasks 1-6 are complete and verified.

---

## Acceptance Criteria

The solution is complete when all of the following are true:

- `memory` remains the small curated layer and is not overloaded with vault/session material.
- `session_search`, `session_list`, and `session_read` are all available and tested.
- `knowledge_search` and `knowledge_read` are available and tested.
- After a rebuild, QMD is present and the vault knowledge layer can recover cleanly.
- The agent can clearly distinguish:
  - who the user is
  - what we already did
  - what the user already knows in their notes/docs

---

## Execution Order

Implement in this order:
1. Task 1
2. Task 2
3. Task 3
4. Task 4
5. Task 5
6. Task 6
7. Task 7 only later if still needed

---

## Verification Commands

Run these at the end:

```bash
cd /app/code
python -m pytest -q tests/tools/test_session_search.py tests/tools/test_knowledge_search_tool.py tests/agent/test_knowledge_backend.py
```

Manual verification:

```bash
command -v qmd
qmd collection list
```

Then in a Hermes session, verify:
- `session_list()` returns recent sessions
- `session_read(session_id=...)` returns a transcript excerpt
- `session_search(query="...")` returns focused summaries
- `knowledge_search(query="...")` returns local vault hits
- `knowledge_read(ref="...")` returns the note content

---

## Final Call

Best solution for this fork: strengthen the missing layers, not the existing one.

Keep built-in memory lean.
Make sessions more explorable.
Make vault knowledge native.
Restore QMD durability so rebuilds stop amputating the third layer.
