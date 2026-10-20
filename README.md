# Agent_skills

Open-source skills for AI agents (Claude Code, Claude apps/Cowork, and other agents that load
`SKILL.md` skills). Each folder is one self-contained skill.

| Skill | What it does |
|---|---|
| [proxmox-lab](proxmox-lab/) | Run a Proxmox VE server or cluster from plain-language requests: VMs, LXC containers, cloud-init templates, snapshots, host bridges/VLANs, SDN, and whole multi-machine labs built from one prompt. |

## Installing a skill

- **Claude Code**: copy the skill folder into `~/.claude/skills/` (all projects) or
  `<project>/.claude/skills/` (one project).
  ```bash
  git clone https://github.com/Abdullah-365/Agent_skills
  cp -r Agent_skills/proxmox-lab ~/.claude/skills/
  ```
- **Claude apps (claude.ai, desktop, Cowork)**: zip the skill folder (the folder itself), then
  **Customize > Skills > + > Create skill > Upload a skill**.
- **Other agents**: copy the folder into the agent's skills directory.

Each skill's own README covers setup and requirements.

## License

[MIT](LICENSE)
