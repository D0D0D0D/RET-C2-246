# RET-C2-246 — Retail In-Store Customer Safety & Physical Anomaly Alert (Multilingual)

> **Category**: Cat 2 (domain workflow)
> **Industry**: RET

## Overview

This agent turns structured in-store incident records and staff-written reports into actionable
safety guidance for retail teams. It classifies reported hazards, applies deterministic severity
scoring, and uses an injected language model to generate location-specific alerts and non-binding
reporting advice in the requested language. Historical incidents can be summarized into
location-level risk trends to help staff focus attention where recurring hazards appear. It
operates on pre-detected, structured incidents rather than raw CCTV or sensor streams, and leaves
legal reporting decisions to human reviewers.

It suits a store team that already has incidents written down and needs them triaged and
communicated: a manager turning wet-floor, fire-hazard and blocked-exit reports into a notice for
staff, a safety lead reviewing what recurs by category and location, or a multilingual floor where
the same notice has to come out in the language of the shift.

The split between what is computed and what is written is the point of the design, because a
safety notice that quietly drops a hazard is worse than no notice:

- **Severity is computed, never asked of the model.** It is `category weight × crowd multiplier`,
  capped at 10 — fire hazard weighs most, an injury or five or more people affected doubles the
  score, and 5+ is high, 3-4 medium, below that low. The same input always produces the same
  score, and the model's own view of severity is not consulted.
- **The model writes prose only**, in the requested language: the alert, the non-binding
  reporting advisory, and the location trend.
- **The output gate checks that high-severity findings survived the prose.** A hazard the
  deterministic layer scored high, absent from the generated text, fails the response rather than
  shipping a notice that reads clean. The check is a preservation check, not a content filter, and
  its authority is the computed scores.
- **A missing model is an error, not a silent downgrade.** With no client injected the run ends as
  `status: error` — a safety agent quietly producing nothing is a provisioning mistake that must be
  visible. A provider *outage* mid-run is different: the deterministic rendering is returned with
  `degradation_reason` set, so the reader can tell computed text from written text.
- **The aggregate log carries counts only** — no incident descriptions, no reporter notes, no
  personal information.

Input is structured: the agent operates on incidents that have already been recorded, not on CCTV,
sensor streams, or free-form prose. It does not determine legal reporting obligations, the
competent authority, or any legal response.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Integration

Provide:

- an AgentCore runtime;
- a `BaseLLM` implementation as `config["llm"]`. No provider client is bundled and no model is
  named. There is **no default that quietly works**: without one, every invocation ends as
  `status: error`;
- caller authentication in front of the entry adapter.

Runtime parameters read from `config/config.yaml`:

| Key | Meaning |
|---|---|
| `locale` (`"ja"`) | Default output language. A request may override it per call with its own `locale` — `ja`, `en`, `vi`, `fil` and others are passed through to the model |
| `max_retry` (`3`), `timeout_seconds` (`30`) | Framework retry budget and timeout |

The template stores nothing between calls. To get a trend, the caller passes
`historical_incidents` in the same request; there is no accumulated history inside the agent.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Supplied by the platform's own package registry at build time. It is not published on public PyPI, so `pip install -e .` alone does not fetch it. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is the agent's import-time dependency: `src/` imports `framework.*` and `shared.*`
directly, so with no SDK installed the process does not start at all. That is what "does not run
standalone" means here — there is no degraded mode that runs without the framework.

With the SDK installed but no platform around it, the standalone server in `src/api/server.py`
does come up, and it is worth being exact about what it can and cannot do:

- `/health` answers `{"status": "ok", "agent": "retail-instore-safety-alert"}`.
- It loads `config/config.yaml` and passes it through unchanged, but it deliberately does **not**
  supply `config["llm"]` — the platform or entry adapter that owns the provider credential does.
  So an authenticated `/invoke` against a bare `uvicorn src.api.server:app` ends as
  `status: error`, by design rather than by omission.
- Entry authentication is enforced: an unauthenticated caller is rejected before the graph runs.

The test suite runs without a platform connection, using an injected fake model.

## Input

`POST /invoke` takes a JSON body with `input` and an optional `session_id`. `input` is itself a
JSON string carrying the incident payload:

| Field | Meaning |
|---|---|
| `incidents` | Required list. Each entry may carry `id`, `location`, `type`, `time`, `description`, `people_affected` and `injury` |
| `locale` | Output language for this call, overriding the configured default |
| `historical_incidents` | Optional prior incidents, used to produce the location trend. Nothing is retained between calls |

`type` is matched against a keyword table in both English and Japanese and mapped to a canonical
category — `fire_hazard`, `egress_obstruction`, `slip_zone` and so on. An unrecognised type
becomes `other`, which carries the lowest weight.

Callers must be authenticated. A verified trust level stamped by upstream middleware is used as
is; otherwise, when `INVOKE_AUTH_TOKEN` is set on the server environment, a matching
`Authorization: Bearer` header promotes the caller to `VERIFIED_EXTERNAL`. A caller that is
neither middleware-verified nor token-authenticated is rejected with **401**, and a caller below
verified trust with **403** — there is no anonymous path into the graph.

```bash
curl -X POST http://127.0.0.1:8000/invoke \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${INVOKE_AUTH_TOKEN}" \
  -d '{
    "input": "{\"locale\":\"en\",\"incidents\":[{\"id\":\"i1\",\"location\":\"Entrance\",\"type\":\"wet floor slip risk\",\"time\":\"2026-06-25T10:00\",\"description\":\"spill near the door\",\"people_affected\":6,\"injury\":false},{\"id\":\"i2\",\"location\":\"Aisle 3\",\"type\":\"fire hazard near shelf\",\"injury\":true}]}",
    "session_id": "store-safety-001"
  }'
```

## Output

The standard AgentCore envelope is preserved; `output` carries:

```json
{
  "status": "success",
  "output": {
    "alert": "[en] 2 location(s) need attention. Mind wet floors and clear aisles.",
    "advisory": "[en] Consider whether a report may be appropriate. This is not a legal determination — the applicable law and authority must be confirmed by a responsible person.",
    "trend": "",
    "incident_log": {
      "incident_count": 2,
      "by_location": {"Entrance": 1, "Aisle 3": 1},
      "by_category": {"slip_zone": 1, "fire_hazard": 1},
      "severity_summary": {"high": 2},
      "generated_locale": "en"
    },
    "degradation_reason": "",
    "ok": true
  }
}
```

`degradation_reason` is empty on the normal path. A non-empty value means `alert` and `advisory`
are the deterministic rendering rather than model-written prose — the one place to look to tell
the two apart.

Values above are illustrative; real output depends on the incidents supplied.

## Security and Limitations

Every node requires verified-external trust, so an anonymous caller is stopped before any node
body runs. The aggregate log is built from counts only, and the output gate scrubs residual
personal information from every string in the result, recursively, before it is returned.

Known limits, recorded rather than hidden:

- **Severity is a rule, not a risk assessment.** It reflects hazard category, injury and crowding
  as recorded. It does not model store layout, footfall, time of day, or anything the record does
  not carry.
- **An unrecognised hazard type scores lowest.** `other` carries weight 1, so a genuine hazard
  described in wording the keyword table does not know is under-weighted rather than flagged.
  Extending the table is part of adapting this template.
- **Preservation checking is not fact checking.** It confirms that a high-severity finding appears
  in the generated text; it does not confirm the surrounding sentence describes it correctly.
- **No history is kept.** Trends exist only for the incidents a caller passes in the same request.
- **The advisory is not a legal determination.** Whether an incident is reportable, to whom, and
  by when, must be confirmed by a responsible person against the applicable law.

Output is machine-generated and assistive. It does not substitute for expert review, and any
decision that matters should be made by a person.

## Release

- Version: 1.0.0
- Category: fixed multi-step domain workflow (Cat 2)
- Generation mode: `llm` (prose only; severity is computed)
- HITL: not enabled
- Persistence: none — no checkpointer is configured, and no incident history is retained
- External API clients: deployment-injected; none bundled

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not — see
*Behaviour without the platform* above.

## Documentation

`docs/02_design.md` carries the node flow, the state schema, the severity model and the security
design. `docs/03_test_spec.md` records the test specification and the verification results,
including a live-model run.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and test specifications
```

See `docs/02_design.md` for the design and `docs/03_test_spec.md` for the test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the knowledge sources and sample data with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.

---
