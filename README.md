# Agent orchestration skills

This repository contains skills for driving interactive coding-agent sessions
from another agent. Both use `agentbus` to detect when a guest agent is working,
blocked, or finished, instead of relying on terminal screen scraping.

## Skills

### [`vessel-agents`](vessel-agents/SKILL.md)

```bash
npx skills install bobisme/skills@vessel-agents
```

Dependencies: [Vessel](https://github.com/bobisme/vessel) and
[agentbus](https://github.com/bobisme/agentbus).

Runs Codex, Claude, Antigravity/Gemini (`agy`), or OpenCode as named
[`vessel`](https://github.com/bobisme/vessel) processes. It covers spawning and
sizing agent TUIs, sending prompts safely, waiting for reliable completion,
collecting results, handling approval prompts, running agents in parallel, and
cleaning them up afterward.

Use this skill when Vessel should own the guest processes or when sessions need
to remain available across multiple turns without depending on a Zellij layout.

### [`zellij-agents`](zellij-agents/SKILL.md)

```bash
npx skills install bobisme/skills@zellij-agents
```

Dependencies: [Zellij](https://zellij.dev/) and
[agentbus](https://github.com/bobisme/agentbus).

Runs Codex, Claude, or OpenCode in dedicated Zellij tabs. It covers launching
agents in a shared `agents` session, sending multiline prompts safely, detecting
turn completion through `agentbus`, following up in the same session, managing
parallel guests, and shutting them down.

Use this skill when agents should be visible and resumable in Zellij, especially
when a person may want to watch or take over a session with
`zellij attach agents`.

Each directory's `SKILL.md` contains the complete workflow, command recipes,
safety notes, and troubleshooting guidance.
