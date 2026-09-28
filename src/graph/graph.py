"""AgentCore Platform v1.0 — RET-C2-246 outer graph.

Cat 2 ChatAgent, Standalone composition: the fixed 5-node backbone
(initialize -> pre_process -> main -> post_process -> finalize) with the three
domain slots filled directly (no inner GraphNode subgraph — the 6 logical steps
are linear; see docs/02_design.md).
"""

from framework.graph.agent_base_graph import AgentBaseGraph
from src.nodes.pre_process_node import PreProcessNode
from src.nodes.main_node import MainNode
from src.nodes.post_process_node import PostProcessNode
from src.schemas.state import State


class RETInStoreSafetyAlertAgent(AgentBaseGraph):
    """Retail in-store customer safety & physical anomaly alert agent (Cat 2 ChatAgent)."""

    @property
    def name(self) -> str:
        return "ret_c2_246"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        # super() injects the framework "initialize" and "finalize" slots.
        super().register_nodes()

        # Domain pipeline slots (required — fill all three):
        self._nodes["pre_process"] = PreProcessNode()
        # LLM injection seam (project standard): the platform / entry adapter puts
        # a BaseLLM into config["llm"] and it reaches the node from here.
        # 🔴 This line used to be `MainNode()`. The node accepted `llm_client=`,
        # but nothing outside the node ever passed it — BaseGraph's only
        # constructor is `__init__(self, config=None)` — so the default build
        # could never reach an LLM and always hit UnconfiguredLLMClient.
        self._nodes["main"] = MainNode(llm_client=self.config.get("llm"))
        self._nodes["post_process"] = PostProcessNode()
