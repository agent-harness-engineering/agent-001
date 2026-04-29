from dataclasses import dataclass, field


@dataclass
class AgentTypeConfig:
    description: str
    prompt_prefix: str = ""
    default_cwd: str | None = None
    capabilities: list[str] = field(default_factory=list)


AGENT_TYPES: dict[str, AgentTypeConfig] = {
    "general": AgentTypeConfig(
        description="General purpose agent with full capabilities",
        capabilities=["chat", "task"],
    ),
    "research": AgentTypeConfig(
        description="Research and codebase exploration agent",
        prompt_prefix=(
            "You are a research agent with live web search. "
            "Web search results and fetched page content have been injected into your context above — "
            "treat them as current, verified information (highest source weight). "
            "Cite the sources you use. Do not say you lack access to current information. "
            "Investigate, summarize, and report findings clearly. Do not take actions — describe what you find. "
        ),
        capabilities=["research", "read", "web_search", "web_fetch"],
    ),
    "sysadmin": AgentTypeConfig(
        description="Systems administration and infrastructure agent",
        prompt_prefix=(
            "You are a sysadmin agent. If URLs were present in your task, fetched content has been injected above. "
            "Diagnose issues, propose fixes, and report system state clearly. "
            "Structure your output with a STATUS section, FINDINGS, and RECOMMENDATIONS. "
        ),
        capabilities=["sysadmin", "ops", "web_fetch"],
    ),
    "status": AgentTypeConfig(
        description="System health and observability agent",
        prompt_prefix=(
            "You are a status agent with live web search. "
            "Web search results have been injected into your context above — use them as current data. "
            "Check the health, state, and metrics of the system. "
            "Report in structured form: HEALTHY/DEGRADED/DOWN per component, with evidence. "
        ),
        capabilities=["monitoring", "read", "web_search", "web_fetch"],
    ),
    "web_search": AgentTypeConfig(
        description="Live web search and retrieval agent",
        prompt_prefix=(
            "You are a web search agent. "
            "Live search results and fetched page content have been injected into your context above — "
            "they are current, real information retrieved moments ago. "
            "Use them as your primary source. Cite titles and URLs. "
            "Summarize what you found clearly and completely. "
            "Do not say you lack access to current information — you have it above. "
        ),
        capabilities=["web_search", "web_fetch", "research"],
    ),
}
