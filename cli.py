"""AGENTIC STAR Marketplace entry adapter for RET-C2-246."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from framework.secrets.base import SecretProvider
from framework.secrets.context import current_secrets
from shared.services.llm.base_llm import BaseLLM

try:
    from framework.utils.config_loader import load_agent_config
except ImportError:  # AgentCore 1.0.0 / 1.0.1 compatibility
    from framework.utils.config_loader import load_config

    def load_agent_config(agent_dir: Path) -> dict[str, Any]:
        config_path = agent_dir / "config" / "config.yaml"
        return load_config(str(config_path)) if config_path.exists() else {}


try:
    from shared.secrets.env_provider import EnvProvider
except ImportError:  # AgentCore 1.0.0 compatibility

    class EnvProvider(SecretProvider):  # type: ignore[no-redef]
        """Minimal process-environment provider used by older SDK images."""

        def __init__(self, *, namespace: str = "", agent_name: str = "") -> None:
            self._namespace = namespace
            self._agent_name = agent_name

        def get(self, key: str, default: str | None = None) -> str | None:
            return os.environ.get(key, default)


from src.graph.graph import RETInStoreSafetyAlertAgent

_NAMESPACE = "ret"
_AGENT_NAME = "RET-C2-246"
_PROVIDER_AGENT_NAME = "ret_c2_246"

_AZURE_OPENAI_SECRET_KEYS = (
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_DEPLOYMENT",
)
_ALLOWED_SECRET_KEYS = frozenset(_AZURE_OPENAI_SECRET_KEYS)


class _AllowlistedEnvironmentProvider(EnvProvider):  # type: ignore[misc]
    """Expose only this agent's declared Azure values from the environment."""

    def __init__(self) -> None:
        super().__init__(namespace=_NAMESPACE, agent_name=_PROVIDER_AGENT_NAME)

    def get(self, key: str, default: str | None = None) -> str | None:
        if key not in _ALLOWED_SECRET_KEYS:
            return default
        value = super().get(key)
        return value if value else default


def _require_clean(provider: SecretProvider, key: str) -> str:
    """Require a clean, non-empty single-line startup value."""

    value = provider.require(key)
    if not value.strip() or value != value.strip() or "\n" in value or "\r" in value:
        raise ValueError(f"{key} must be a clean non-empty single-line value")
    return value


def _require_azure_endpoint(provider: SecretProvider) -> str:
    """Reject ``AZURE_OPENAI_ENDPOINT`` values that are not a bare Azure resource endpoint.

    ``AzureOpenAIClient`` appends its own ``/openai/v1/`` path, so anything
    beyond a scheme and host silently produces a broken request URL, and the
    failure surfaces inside ``complete()``. A node that swallows that exception
    degrades quietly while the invocation still reports ``status: success``,
    which is why this has to be a startup check. The value is reported here,
    never repaired.
    """

    endpoint = _require_clean(provider, "AZURE_OPENAI_ENDPOINT")
    parts = urlsplit(endpoint)
    try:
        port = parts.port
    except ValueError:
        port = -1
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or port == -1
        or parts.path.strip("/")
        or parts.query
        or parts.fragment
        or any(character.isspace() for character in endpoint)
    ):
        raise ValueError(
            "Marketplace environment variable 'AZURE_OPENAI_ENDPOINT' must be a bare Azure "
            "resource endpoint -- scheme and host only, with no path (such as "
            "'/openai'), query or fragment"
        )
    return endpoint


class AzureLLM(BaseLLM):  # type: ignore[misc]
    """Create an Azure client from the currently bound secrets for every call."""

    def __init__(self, *, tools: tuple[Any, ...] = ()) -> None:
        self._tools = tools

    def _client(self) -> BaseLLM:
        # Keep the optional Azure integration lazy so SDK 1.0.0 can import this
        # module even though that image does not contain AzureOpenAIClient.
        from shared.services.llm.azure_openai_client import AzureOpenAIClient

        secrets = current_secrets()
        client = AzureOpenAIClient(
            {
                "api_key": secrets.require("AZURE_OPENAI_API_KEY"),
                "azure_endpoint": secrets.require("AZURE_OPENAI_ENDPOINT"),
                "azure_deployment": secrets.require("AZURE_OPENAI_DEPLOYMENT"),
            }
        )
        return client.bind_tools(list(self._tools)) if self._tools else client

    def complete(self, messages: list[Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._client().complete(messages))

    def stream(self, messages: list[Any]) -> Iterator[str]:
        yield from self._client().stream(messages)

    def bind_tools(self, tools: list[Any]) -> "AzureLLM":
        return type(self)(tools=tuple(tools))


class MarketplaceGraph(RETInStoreSafetyAlertAgent):
    """Marketplace-only input boundary and Azure dependency wiring."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config=config)
        # The Marketplace runner compiles before provision_secrets(). Keep one
        # connection-free wrapper available so register_nodes() can use the
        # graph's existing config["llm"] injection seam during that compile.
        self._marketplace_llm = AzureLLM()

    def register_nodes(self) -> None:
        self.config["llm"] = self._marketplace_llm
        super().register_nodes()

    def invoke(
        self,
        user_input: str,
        session_id: str = "",
        ctx: Any | None = None,
        input_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Runner conversation history is untrusted free text outside the S-2
        # fields. Rebuild context solely from adapter-owned configuration.
        del input_context
        adapter_context = {"locale": str(self.config.get("locale") or "ja")}
        return cast(
            dict[str, Any],
            super().invoke(
                user_input,
                session_id=session_id,
                ctx=ctx,
                input_context=adapter_context,
            ),
        )

    def provision_secrets(self, provider: SecretProvider) -> None:
        # Discard the runner's dotenv-capable/unrestricted provider and bind a
        # provider that can expose only the three declared Azure values.
        del provider
        environment_provider = _AllowlistedEnvironmentProvider()
        for key in _AZURE_OPENAI_SECRET_KEYS:
            _require_clean(environment_provider, key)

        self.config["llm"] = self._marketplace_llm
        _require_azure_endpoint(environment_provider)
        super().provision_secrets(environment_provider)


def main() -> None:
    # AgentCore 1.0.0 has no shared.bootstrap module. Marketplace startup uses
    # a newer image, so import the runner only at the executable boundary.
    from shared.bootstrap import run_agent_marketplace

    config = load_agent_config(Path(__file__).resolve().parent)
    run_agent_marketplace(
        MarketplaceGraph,
        agent_name=_AGENT_NAME,
        namespace=_NAMESPACE,
        config=config,
    )


if __name__ == "__main__":
    main()
