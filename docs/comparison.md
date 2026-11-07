# Comparison

Several community projects generate images through the Codex Images endpoint on a ChatGPT plan. They use the same model, so **image quality is the same**. They differ in how they deploy and how they deliver results. This comparison reflects each project's public source as of November 2026. Check their repositories for current features.

| | codex-images-mcp | [codex-imagegen-mcp](https://github.com/ShalomObongo/codex-imagegen-mcp) | [codex-imagen](https://github.com/VelmoAI/codex-imagen) (upstream) |
|---|---|---|---|
| Language | Python | TypeScript | Python |
| Transport | stdio **and** Streamable HTTP | stdio | stdio |
| Self-hosting with built-in auth | Yes (bearer, refuses public bind without it) | No | No |
| Engine | Native Codex Images endpoint | Native Codex Images endpoint | Codex Responses with a prompt-rewriting model |
| Prompts | Verbatim | Verbatim, plus aspect-ratio guidance | Rewritten by a reasoning model |
| Transparency | Native alpha | Native alpha, plus local chroma-key removal | Chroma-key post-processing |
| Edit masks | Yes | No separate mask input | Yes |
| Exact output sizes | `exact` / `fit` / `cover` | Aspect ratio; resizing left to the client | Backend sizes |
| Results | `resource_link` artifacts with SHA-256, MIME and dimensions; signed downloads | Files on disk, inline previews, resources | Files on disk |
| Remote URL inputs | HTTPS, public addresses only, pinned IP, no redirects | HTTP(S), redirects followed | Not supported |
| ChatGPT sign-in | Own device-code session; Codex login read-only | Own browser or device login; borrowed logins read-only | Codex CLI login |
| Client installers | 5 clients | 25+ clients | 5 clients |
| Pi / Gentle Shell extension | Yes | No | No |
| License | MIT | Apache-2.0 | MIT |

## When to use which

- **codex-imagegen-mcp** if you want the widest client support with a one-command installer on one machine. It is a polished, active project.
- **codex-images-mcp** if you want **one server for several machines or agents** (a VPS, a home server, remote coding agents), images you can verify and fetch by URI, exact sizes and masks, or a Pi / Gentle Shell integration.
