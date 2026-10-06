# ue58-probe

Read-only probe for Epic's Unreal MCP (UE 5.8+). Measures how many tokens tool discovery costs an agent and builds a compact local signature catalog.

```
python probe.py --url http://127.0.0.1:8000/mcp --out Saved/hus/probe
```

- Calls only `tools/list`, `list_toolsets` and `describe_toolset`; editor state is not read or changed.
- Handles both 5.8.0 (SSE) and 5.8.1+ (JSON) reply framing.
- `summary.json` holds numbers only and is safe to share.
- `raw/` and `catalog.txt` contain Epic-derived text: keep them local, never commit them (UE EULA).

Details (in Russian): [docs/research/ue58-phase0.md](../../docs/research/ue58-phase0.md).
