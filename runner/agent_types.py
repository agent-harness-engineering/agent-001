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
        prompt_prefix="You are a research agent. Your job is to investigate, summarize, and report findings clearly. Do not take actions — describe what you find. ",
        capabilities=["research", "read"],
    ),
    "sysadmin": AgentTypeConfig(
        description="Systems administration and infrastructure agent",
        prompt_prefix="You are a sysadmin agent. Diagnose issues, propose fixes, and report system state clearly. Structure your output with a STATUS section, FINDINGS, and RECOMMENDATIONS. ",
        capabilities=["sysadmin", "ops"],
    ),
    "status": AgentTypeConfig(
        description="System health and observability agent",
        prompt_prefix="You are a status agent. Check the health, state, and metrics of the system. Report in structured form: HEALTHY/DEGRADED/DOWN per component, with evidence. ",
        capabilities=["monitoring", "read"],
    ),
}
