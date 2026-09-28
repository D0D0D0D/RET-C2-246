"""AgentCore Platform v1.0 — RET-C2-246 HTTP entry point.

Entry points are adapters only — no business logic here. For platform-level
routing, AgentGateway calls agent.invoke() directly.

Runtime settings come from config/config.yaml and are passed through unchanged,
matching AgentRegistry._compile_and_cache()'s own config_dir/"config.yaml"
convention. Without this the standalone adapter constructed the agent with no
config at all, so max_retry and the config["llm"] injection seam had no way to
reach the graph on this path.

This standalone server does not itself supply config["llm"] — the platform /
entry adapter that owns the provider credential does. With no client injected
the agent fails closed (status: error) rather than degrading silently; see the
degradation policy in src/nodes/main_node.py.

Entry authentication is implemented by entry_adapter.auth:
  - a verified middleware trust (request.state.trust_level) is used as-is;
  - otherwise INVOKE_AUTH_TOKEN Bearer auth promotes ANONYMOUS →
    VERIFIED_EXTERNAL (_resolve_trust raises 401 on token mismatch);
  - an unauthenticated caller (no verified middleware trust AND no token
    promotion) is rejected with 401. The old fallback that defaulted such
    callers to ANONYMOUS was fail-open (S-1 bypass) and is removed.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from framework.utils.config_loader import load_config
from shared.secrets import factory as secrets_factory

from src.entry_adapter.auth import _require_verified_trust, _resolve_trust
from src.graph.graph import RETInStoreSafetyAlertAgent

app = FastAPI(title="Agent")

# Absent config.yaml is tolerated, matching the registry's own exists() guard.
_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"
_config = load_config(str(_CONFIG_PATH)) if _CONFIG_PATH.exists() else {}

agent = RETInStoreSafetyAlertAgent(config=_config)
agent.compile()
# Replace namespace/agent_name to match the agent's manifest values.
agent.provision_secrets(secrets_factory(namespace="retail", agent_name="retail-instore-safety-alert"))


def _ctx(request: Request, session_id: str) -> InvocationContext:
    """Build the InvocationContext AFTER entry authentication.

    Scope note: the entry_adapter part also exposes a tenant/org scope contract,
    but this template has NO scope model. In-store safety alerting operates on
    the per-request incident payload only; there is no code path that reads a
    request tenant/org scope. Enforcing authenticate_scope() would gate on a
    scope nothing consumes (STG-default theater; a "default" fallback would
    silently match everything), so only the auth core (_resolve_trust /
    _require_verified_trust) is wired.
    """
    trust, token_authenticated = _resolve_trust(request)
    # Unauthenticated = trust is still ANONYMOUS after resolution and no token
    # promotion. Test the trust VALUE, not attribute presence — a middleware
    # that explicitly stamps ANONYMOUS must still be treated as unauthenticated
    # (→ 401), not silently accepted as "verified because the attr exists".
    if trust is TrustLevel.ANONYMOUS and not token_authenticated:
        raise HTTPException(status_code=401, detail="Authentication is required.")
    _require_verified_trust(trust)
    return InvocationContext(
        session_id=session_id or str(uuid4()),
        caller_trust_level=trust,
        caller_id=getattr(request.state, "caller_id", ""),
    )


class InvokeRequest(BaseModel):
    input: str
    session_id: str = ""


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request):
    ctx = _ctx(request, req.session_id)
    with bound_secrets(agent._secrets_provider):
        return agent.invoke(req.input, ctx=ctx)


@app.get("/health")
def health():
    return {"status": "ok", "agent": "retail-instore-safety-alert"}
