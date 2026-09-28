from __future__ import annotations

import sys
import types
from typing import Any

import pytest

import cli
from framework.secrets.base import MissingSecret, SecretProvider
from src.graph.graph import RETInStoreSafetyAlertAgent
from src.services.llm_client import BaseLLMSafetyContentAdapter, UnconfiguredLLMClient, resolve_llm_client


class _EmptyProvider(SecretProvider):
    def get(self, key: str, default: str | None = None) -> str | None:
        del key
        return default


def _set_valid_azure_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "key")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://azure.example.test")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "deployment")


def test_invoke_rebuilds_context_without_runner_conversation_history(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def capture_parent_invoke(
        self: RETInStoreSafetyAlertAgent,
        user_input: str,
        session_id: str = "",
        ctx: Any | None = None,
        input_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del self, user_input, session_id, ctx
        captured["input_context"] = input_context
        return {"status": "captured"}

    monkeypatch.setattr(RETInStoreSafetyAlertAgent, "invoke", capture_parent_invoke)
    graph = cli.MarketplaceGraph(config={"locale": "vi"})
    runner_context = {
        "conversation_history": [{"role": "user", "content": "Customer alice@example.test was near aisle 3"}]
    }

    graph.invoke('{"incidents": []}', input_context=runner_context)

    assert captured["input_context"] == {"locale": "vi"}
    assert "conversation_history" not in captured["input_context"]
    assert "alice@example.test" not in repr(captured["input_context"])


@pytest.mark.parametrize("key", cli._AZURE_OPENAI_SECRET_KEYS)
@pytest.mark.parametrize("bad_value", [None, "", "   "])
def test_provision_secrets_fails_closed_for_missing_or_blank_azure_values(
    monkeypatch: pytest.MonkeyPatch, key: str, bad_value: str | None
) -> None:
    _set_valid_azure_environment(monkeypatch)
    if bad_value is None:
        monkeypatch.delenv(key)
    else:
        monkeypatch.setenv(key, bad_value)

    expected_error = MissingSecret if not bad_value else ValueError
    with pytest.raises(expected_error):
        cli.MarketplaceGraph().provision_secrets(_EmptyProvider())


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.cognitiveservices.azure.com/openai/responses?api-version=2025-04-01-preview",
        "https://example.services.ai.azure.example/openai",
        "https://example.services.ai.azure.example/v1",
        "https://example.services.ai.azure.example?api-version=2024-10-21",
        "example.services.ai.azure.example",
    ],
    ids=["api-path", "openai-path", "base-path", "query-only", "missing-scheme"],
)
def test_unusable_azure_endpoint_shape_fails_during_startup(
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    """AzureOpenAIClient appends its own path, so anything but a bare endpoint breaks.

    The resulting error surfaces inside complete(); a node that swallows it
    degrades silently while the invocation still reports status: success, so the
    adapter has to reject the shape at startup instead.
    """

    _set_valid_azure_environment(monkeypatch)
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", endpoint)

    with pytest.raises(ValueError, match="bare"):
        cli.MarketplaceGraph().provision_secrets(_EmptyProvider())


@pytest.mark.parametrize("bad_value", ["key\ncontinued", " endpoint\r"])
def test_provision_secrets_rejects_newlines(monkeypatch: pytest.MonkeyPatch, bad_value: str) -> None:
    _set_valid_azure_environment(monkeypatch)
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", bad_value)

    with pytest.raises(ValueError, match="AZURE_OPENAI_API_KEY"):
        cli.MarketplaceGraph().provision_secrets(_EmptyProvider())


def test_provision_secrets_injects_base_llm_through_existing_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_valid_azure_environment(monkeypatch)
    graph = cli.MarketplaceGraph()
    graph.compile()  # Marketplace runner ordering: compile, then provision.

    graph.provision_secrets(_EmptyProvider())

    assert graph.config["llm"] is graph._marketplace_llm
    resolved = resolve_llm_client(graph.config["llm"])
    assert isinstance(resolved, BaseLLMSafetyContentAdapter)
    assert not isinstance(resolved, UnconfiguredLLMClient)
    assert isinstance(graph._nodes["main"]._llm, BaseLLMSafetyContentAdapter)


def test_azure_llm_resolves_secrets_and_constructs_client_for_each_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_resolutions = 0
    client_configs: list[dict[str, str]] = []

    class Secrets(_EmptyProvider):
        def get(self, key: str, default: str | None = None) -> str | None:
            values = {
                "AZURE_OPENAI_API_KEY": "key",
                "AZURE_OPENAI_ENDPOINT": "endpoint",
                "AZURE_OPENAI_DEPLOYMENT": "deployment",
            }
            return values.get(key, default)

    def resolve_secrets() -> SecretProvider:
        nonlocal secret_resolutions
        secret_resolutions += 1
        return Secrets()

    class FakeAzureOpenAIClient:
        def __init__(self, config: dict[str, str]) -> None:
            client_configs.append(config)

        def complete(self, messages: list[Any]) -> dict[str, Any]:
            del messages
            return {"content": "ok", "tool_calls": [], "model": "fake"}

    fake_module = types.ModuleType("shared.services.llm.azure_openai_client")
    fake_module.AzureOpenAIClient = FakeAzureOpenAIClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, fake_module.__name__, fake_module)
    monkeypatch.setattr(cli, "current_secrets", resolve_secrets)
    llm = cli.AzureLLM()

    llm.complete([])
    llm.complete([])

    assert secret_resolutions == 2
    assert len(client_configs) == 2
    assert client_configs[0] is not client_configs[1]
