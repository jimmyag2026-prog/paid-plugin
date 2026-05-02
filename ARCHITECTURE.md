# PAID v1 Sprint — Technical Architecture

> Designed: 2026-05-01 18:05
> Reference: [05_paid_prd_v1.md](../../research/05_paid_prd_v1.md)
> Scope: 2-hour sprint scope (NOT full v1)

---

## Top-level Architecture

PAID is a **Hermes plugin** registered via `~/.hermes/plugins/paid-v1/`.
It wires into Hermes via 4 hooks (verified to fire):
- `on_session_start` (best-effort, may have null source — DO NOT depend)
- `pre_llm_call` ← **primary identification + context injection point**
- `post_llm_call` ← audit logging point
- `pre_tool_call` ← future tool gating (v2)

State lives at `~/.hermes/paid/` (separate from plugin code).

---

## Module Layout

```
v1-sprint/
├── plugin.yaml             # Hermes plugin manifest
├── __init__.py             # entry point: register(ctx) + hook glue
├── paid/                   # core modules
│   ├── __init__.py         # package init
│   ├── storage.py          # Module S — paths + IO
│   ├── identity.py         # Module I — owner + counterparty
│   ├── classifier.py       # Module C — 3-state classify (LLM call)
│   ├── retrieval.py        # Module R — SOP keyword search (FTS5 stretch)
│   ├── decision.py         # Module D — 3-state rules + context shaping
│   ├── audit.py            # Module A — append-only log
│   └── hermes_io.py        # Module H — Hermes API helpers (LLM, send)
└── tests/
    ├── test_identity.py
    ├── test_classifier.py
    ├── test_decision.py
    └── test_integration.py
```

---

## Module Contracts (interfaces — subagents MUST follow these)

### Module S · `paid.storage`
**Responsibility**: paths, file IO, JSON read/write, directory ensure

```python
PAID_DIR: Path  # = ~/.hermes/paid

def ensure_dirs() -> None
def read_json(path: Path) -> dict | None  # None if missing
def write_json(path: Path, data: dict) -> None
def read_text(path: Path) -> str | None
def append_jsonl(path: Path, entry: dict) -> None
```

### Module I · `paid.identity`
**Responsibility**: owner detection, counterparty profile load/create

```python
def load_owner() -> Owner | None  # reads owner.json
def is_owner(platform: str, sender_id: str) -> bool
def load_counterparty(platform: str, sender_id: str) -> Counterparty | None
def ensure_counterparty(platform: str, sender_id: str, display_name: str = "") -> Counterparty
    # creates with default unauthorized profile if missing

@dataclass
class Owner:
    owner_id: str
    identities: list[dict]  # [{"platform": "telegram", "user_id": "..."}]

@dataclass
class Counterparty:
    cp_id: str  # platform_userid composite, e.g. "telegram_854066391"
    platform: str
    user_id: str
    display_name: str
    role: str  # "junior" | "external" | "ignored" | "blocked" | "pending"
    topics_allowed: list[str]
    topics_always_escalate: list[str]
    web_search_allowed: bool
    notes: str
```

### Module C · `paid.classifier`
**Responsibility**: classify junior message → structured decision input

```python
def classify(
    user_message: str,
    counterparty: Counterparty,
    owner_name: str,
    sop_excerpt: str,  # max ~2KB of relevant SOP
) -> Classification

@dataclass
class Classification:
    topic: str
    stakes: str  # "low" | "medium" | "high"
    in_scope: bool
    is_blacklisted: bool
    confidence: float  # 0.0-1.0
    needs_retrieval: bool
    suggested_queries: list[str]
    draft_answer: str
    reasoning: str
```

Implementation: 1 LLM call to Hermes-configured model with strict JSON output instructions.
Use `paid.hermes_io.call_llm()` (provided by Module H).

### Module R · `paid.retrieval`
**Responsibility**: simple keyword retrieval over sop.md + persona.md
*(v1 sprint = simple substring/keyword match, FTS5 stretch goal)*

```python
def retrieve_sop_context(query: str, max_chars: int = 2000) -> str
    # Returns concatenated relevant chunks from sop.md
    # v1 sprint: simple substring matching on paragraphs
    # Return "" if no SOP file or no matches
```

### Module D · `paid.decision`
**Responsibility**: 3-state rules + context formatting for pre_llm_call return

```python
def decide_action(
    classification: Classification,
    counterparty: Counterparty,
    user_message: str = "",        # raw text — checked against hard blacklist
) -> Action

def shape_context(
    action: Action,
    classification: Classification,
    persona: str,
    counterparty: Counterparty,
    sop_excerpt: str = "",
    owner_name: str = "the owner",
    lang: str = "en",              # "zh" | "en" — picks Chinese vs English copy
) -> str
    # Returns the context string that pre_llm_call hook returns to Hermes.

def detect_lang(text: str) -> str   # heuristic CN/EN detector for caller use

@dataclass
class Action:
    state: str  # "direct" | "request" | "decline"
    reason: str  # human-readable rationale (for audit)
```

Decision rules (sprint version, evaluated in order):
- **0.** Hard global blacklist keyword in `user_message` → request
  *(credentials / wifi / finance / equity / hiring / legal / etc.;
  catches the "wifi password classified as low-stakes" failure mode)*
- 1. `is_blacklisted=True` → decline
- 2. `in_scope=False` → request
- 3. `stakes=high` → request
- 4. `confidence > _CONFIDENCE_THRESHOLD_DIRECT (0.75) AND stakes=low AND in_scope=True` → direct
- 5. otherwise → request

`_CONFIDENCE_THRESHOLD_DIRECT` is a module constant; will lift to
`settings.json` once that config layer exists (see
`design/01_review_decisions.md §2`).

Context shape (Chinese / English chosen by `lang`):
- direct: persona + SOP excerpt + draft + bilingual signoff instruction (sign as `— {owner}'s PAID`)
- request: `IGNORE the user question. Reply EXACTLY with: '<warm placeholder with greeting + ETA + escalation path>'`
- decline: `IGNORE the user question. Reply EXACTLY with: '<warm decline with redirect to @owner>'`

### Module A · `paid.audit`
**Responsibility**: append-only log writer

```python
def log_action(
    session_id: str,
    counterparty: Counterparty | None,  # None if pre-identification
    junior_msg: str,
    classification: Classification | None,
    action: Action | None,
    extra: dict | None = None,
) -> None
    # appends one line to ~/.hermes/paid/audit_log.jsonl
```

### Module H · `paid.hermes_io`
**Responsibility**: call Hermes-configured LLM; placeholder for outbound send

```python
def call_llm(
    prompt: str,
    system: str = "",
    json_mode: bool = False,
    timeout: float = 60.0,
    temperature: float | None = None,   # classifier passes 0.1 for determinism
) -> str
    # Reads ~/.hermes/config.yaml for model + api_key + base_url
    # Returns response text

def send_dm(platform: str, user_id: str, message: str) -> dict
    # STUB — raises NotImplementedError in v1 sprint.
    # Required by J3 (approval cards), J4 (discovery cards), IN-5 (progress
    # push). Wire to Hermes gateway delivery API in Week 2; see
    # design/01_review_decisions.md §2.1.
```

---

## Data Files (state at `~/.hermes/paid/`)

```
~/.hermes/paid/
├── owner.json            # Module I reads
├── persona.md            # Module D reads
├── sop.md                # Module R reads
├── settings.json         # Module D reads (thresholds)
└── counterparties/
    └── <cp_id>/
        └── profile.json  # Module I reads/writes
```

---

## Hermes Plugin Glue (`__init__.py`)

```python
def register(ctx):
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("post_llm_call", on_post_llm_call)

def on_pre_llm_call(**kwargs) -> dict | None:
    sender_id = kwargs.get("sender_id") or ""
    platform = kwargs.get("platform") or ""
    user_message = kwargs.get("user_message") or ""
    session_id = kwargs.get("session_id") or ""
    
    # Owner short-circuit
    if identity.is_owner(platform, sender_id):
        return None  # let Hermes do its normal thing
    
    # Counterparty flow
    cp = identity.ensure_counterparty(platform, sender_id)
    if cp.role in ("ignored", "blocked"):
        # silent: tell Hermes to make response empty
        return {"context": "Reply ONLY with: ''. Nothing else."}
    
    sop_excerpt = retrieval.retrieve_sop_context(user_message)
    classification = classifier.classify(user_message, cp, owner_name, sop_excerpt)
    action = decision.decide_action(classification, cp)
    context = decision.shape_context(action, classification, persona, cp)
    
    audit.log_action(session_id, cp, user_message, classification, action,
                     extra={"context_injected": context[:200]})
    
    return {"context": context}

def on_post_llm_call(**kwargs):
    # Just log the assistant response for audit
    audit.log_action(
        kwargs.get("session_id") or "",
        None,  # cp not re-resolved here for simplicity
        "",
        None, None,
        extra={"assistant_response": (kwargs.get("assistant_response") or "")[:300]},
    )
```

---

## Concurrency / Parallel Dev Strategy

| Subagent | Modules | Files written |
|---|---|---|
| Subagent 1 (Storage+Identity+Audit) | S, I, A | `paid/storage.py`, `paid/identity.py`, `paid/audit.py`, tests |
| Subagent 2 (Classifier+Hermes IO) | C, H | `paid/classifier.py`, `paid/hermes_io.py`, tests |
| Subagent 3 (Decision+Retrieval) | D, R | `paid/decision.py`, `paid/retrieval.py`, tests |
| Main agent (after subagents return) | Plugin glue + integration | `__init__.py`, `plugin.yaml`, integration test |

**No subagent writes to the same file.** Each subagent only depends on `paid/storage.py` (Module S) and `ARCHITECTURE.md` (interface contracts).

---

## v1 Sprint Acceptance Criteria

Phase 4 live test passes if:

1. ✅ Plugin loads in Hermes (no import errors, shows in `hermes plugins list`)
2. ✅ Send a CLI message via `hermes chat -q "test"` — plugin hooks fire
3. ✅ Send a Telegram message from non-owner account — gets PAID-styled response (signed "Jimmy's PAID")
4. ✅ Send a Telegram message from owner account — gets normal Hermes response (no PAID interference)
5. ✅ audit_log.jsonl receives entries
6. ⚠️ Stretch: classification actually decides direct vs request based on message content

If 1-5 pass, sprint is **green**.
If only 1-3 pass, sprint is **yellow** (foundation correct but classifier broken).
If 1-2 fail, sprint is **red** (something is fundamentally wrong).
