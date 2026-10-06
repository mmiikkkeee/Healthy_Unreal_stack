#!/usr/bin/env python3
"""Read-only probe for Epic's Unreal MCP (UE 5.8+).

Connects to the editor's MCP endpoint, measures what tool discovery costs an
agent (tools/list, list_toolsets, describe_toolset for every toolset), and
builds a compact local signature catalog that can replace describe_toolset
calls in agent context.

Only discovery meta-tools are called; no editor state is read or changed.

Outputs (written to --out, default ./Saved/hus/probe next to the current dir):
  raw/        verbatim server responses — contain Epic text, never commit them
  catalog.txt compact signatures generated from the raw responses (also Epic-
              derived: keep it local, it is regenerated on each machine)
  summary.json numbers only — safe to share

Usage:
  python probe.py [--url http://127.0.0.1:8000/mcp] [--out DIR]
Stdlib only, Python 3.9+.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

PROTOCOL_VERSION = "2025-06-18"
CHARS_PER_TOKEN = 4.0  # rough for compact JSON/prose; summary also keeps raw chars


def est_tokens(chars):
    return round(chars / CHARS_PER_TOKEN)


class McpHttpClient:
    """Minimal Streamable HTTP MCP client: JSON or SSE replies, optional session id."""

    def __init__(self, url, timeout):
        self.url = url
        self.timeout = timeout
        self.session_id = None
        self.next_id = 1

    def _post(self, payload):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        req = urllib.request.Request(self.url, json.dumps(payload).encode("utf-8"), headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                self.session_id = sid
            body = resp.read().decode("utf-8")
            ctype = resp.headers.get("Content-Type", "")
        if not body.strip():
            return None
        if "text/event-stream" in ctype or body.lstrip().startswith(("event:", "data:")):
            return _last_sse_message(body)
        return json.loads(body)

    def request(self, method, params=None):
        msg = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        self.next_id += 1
        if params is not None:
            msg["params"] = params
        started = time.perf_counter()
        reply = self._post(msg)
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        if reply is None:
            raise RuntimeError(f"{method}: empty reply")
        if "error" in reply:
            raise RuntimeError(f"{method}: {reply['error']}")
        return reply["result"], elapsed_ms

    def notify(self, method):
        self._post({"jsonrpc": "2.0", "method": method})

    def call_tool(self, name, arguments):
        return self.request("tools/call", {"name": name, "arguments": arguments})


def _last_sse_message(body):
    message = None
    data_lines = []
    for line in body.splitlines() + [""]:
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        elif not line.strip() and data_lines:
            candidate = json.loads("\n".join(data_lines))
            if "result" in candidate or "error" in candidate:
                message = candidate
            data_lines = []
    return message


def result_text(result):
    """What the model sees for a text-only tool result."""
    if result.get("structuredContent") is not None:
        return json.dumps(result["structuredContent"], separators=(",", ":"), ensure_ascii=False)
    return "".join(block.get("text", "") for block in result.get("content", []) if block.get("type") == "text")


def first_required_arg(tool):
    schema = tool.get("inputSchema") or {}
    required = schema.get("required") or list((schema.get("properties") or {}).keys())
    return required[0] if required else None


def parse_toolset_names(text):
    names = []
    for line in text.splitlines():
        match = re.match(r"^- ([A-Za-z_][\w.]*):", line)
        if match and match.group(1) not in names:
            names.append(match.group(1))
    return names


def find_tool_dicts(node):
    """Collect dicts that look like tool definitions anywhere in a describe payload."""
    found = []
    if isinstance(node, dict):
        if "name" in node and ("inputSchema" in node or "parameters" in node or "input_schema" in node):
            found.append(node)
        else:
            for value in node.values():
                found.extend(find_tool_dicts(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(find_tool_dicts(item))
    return found


def schema_type(prop):
    if not isinstance(prop, dict):
        return "any"
    if "$ref" in prop:
        return prop["$ref"].rsplit("/", 1)[-1]
    kind = prop.get("type")
    if isinstance(kind, list):
        kind = "|".join(k for k in kind if k != "null")
    if kind == "array":
        return f"[{schema_type(prop.get('items'))}]"
    if "enum" in prop:
        return "|".join(str(v) for v in prop["enum"][:6])
    if kind is None and ("anyOf" in prop or "oneOf" in prop):
        return "|".join(schema_type(p) for p in (prop.get("anyOf") or prop.get("oneOf")) if p.get("type") != "null")
    return kind or "object"


def first_sentence(text, limit=90):
    text = " ".join((text or "").split())
    match = re.match(r"(.+?[.!?])(\s|$)", text)
    sentence = match.group(1) if match else text
    return sentence if len(sentence) <= limit else sentence[: limit - 1] + "…"


def signature(toolset, tool):
    schema = tool.get("inputSchema") or tool.get("parameters") or tool.get("input_schema") or {}
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    args = ", ".join(f"{name}{'' if name in required else '?'}: {schema_type(prop)}" for name, prop in props.items())
    out = tool.get("outputSchema") or tool.get("output_schema")
    ret = ""
    if isinstance(out, dict):
        inner = (out.get("properties") or {}).get("returnValue", out)
        ret = f" -> {schema_type(inner)}"
    return f"{toolset}.{tool['name']}({args}){ret}  # {first_sentence(tool.get('description'))}"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://127.0.0.1:8000/mcp")
    parser.add_argument("--out", default=os.path.join("Saved", "hus", "probe"))
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()

    raw_dir = os.path.join(args.out, "raw")
    os.makedirs(raw_dir, exist_ok=True)
    client = McpHttpClient(args.url, args.timeout)

    try:
        init, init_ms = client.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "hus-probe", "version": "0.1"},
        })
    except (urllib.error.URLError, ConnectionError) as exc:
        sys.exit(f"Cannot reach {args.url}: {exc}\nIs the editor running with the ModelContextProtocol plugin enabled?")
    client.notify("notifications/initialized")

    tools_list, list_ms = client.request("tools/list")
    tools = tools_list.get("tools", [])
    tools_list_json = json.dumps(tools, separators=(",", ":"), ensure_ascii=False)
    instructions = init.get("instructions") or ""
    _write(raw_dir, "tools_list.json", json.dumps(tools_list, indent=1, ensure_ascii=False))

    by_name = {t["name"]: t for t in tools}
    summary = {
        "url": args.url,
        "server": init.get("serverInfo"),
        "protocol": init.get("protocolVersion"),
        "tool_search_mode": set(by_name) >= {"list_toolsets", "describe_toolset", "call_tool"},
        "tools_list": {"count": len(tools), "chars": len(tools_list_json), "est_tokens": est_tokens(len(tools_list_json)),
                       "instructions_chars": len(instructions), "ms": list_ms},
    }

    if not summary["tool_search_mode"]:
        print("Tool search is off (all tools are native); measuring tools/list only.")
        _finish(args.out, summary, [])
        return

    listing, listing_ms = client.call_tool("list_toolsets", {})
    listing_text = result_text(listing)
    _write(raw_dir, "list_toolsets.txt", listing_text)
    names = parse_toolset_names(listing_text)
    summary["list_toolsets"] = {"chars": len(listing_text), "est_tokens": est_tokens(len(listing_text)),
                                "listed_names": len(names), "ms": listing_ms}

    arg_name = first_required_arg(by_name["describe_toolset"]) or "toolset_name"
    rows, catalog = [], []
    for name in names:
        try:
            described, ms = client.call_tool("describe_toolset", {arg_name: name})
        except RuntimeError as exc:
            rows.append({"toolset": name, "error": str(exc)[:200]})
            continue
        text = result_text(described)
        if described.get("isError"):
            rows.append({"toolset": name, "error": text[:200]})
            continue
        _write(raw_dir, f"describe_{name}.json", text)
        try:
            tool_defs = find_tool_dicts(json.loads(text))
        except json.JSONDecodeError:
            tool_defs = []
        sigs = [signature(name, t) for t in tool_defs]
        catalog.extend(sigs)
        sig_chars = sum(len(s) + 1 for s in sigs)
        out_schema_chars = sum(len(json.dumps(t.get("outputSchema") or {}, separators=(",", ":"))) for t in tool_defs)
        rows.append({
            "toolset": name, "tools": len(tool_defs), "chars": len(text), "est_tokens": est_tokens(len(text)),
            "output_schema_share": round(out_schema_chars / max(1, len(text)), 3),
            "over_50k_chars": len(text) > 50_000, "catalog_chars": sig_chars,
            "catalog_est_tokens": est_tokens(sig_chars), "ms": ms,
        })
        print(f"{name:45} {len(tool_defs):4} tools {len(text):>8} chars -> catalog {sig_chars:>7} chars  {ms} ms")

    _write(args.out, "catalog.txt", "\n".join(catalog) + "\n")
    _finish(args.out, summary, rows)


def _finish(out_dir, summary, rows):
    ok = [r for r in rows if "error" not in r]
    describe_total = sum(r["chars"] for r in ok)
    catalog_total = sum(r["catalog_chars"] for r in ok)
    summary["describe_toolset"] = {
        "toolsets": len(ok), "errors": len(rows) - len(ok), "tools": sum(r["tools"] for r in ok),
        "total_chars": describe_total, "total_est_tokens": est_tokens(describe_total),
        "median_chars": sorted(r["chars"] for r in ok)[len(ok) // 2] if ok else 0,
        "over_50k_chars": [r["toolset"] for r in ok if r["over_50k_chars"]],
        "catalog_total_chars": catalog_total, "catalog_total_est_tokens": est_tokens(catalog_total),
        "compression": round(describe_total / max(1, catalog_total), 1),
    }
    summary["per_toolset"] = rows
    summary["note"] = "est_tokens = chars/4, rough; Claude's tokenizer may differ by ±30%."
    _write(out_dir, "summary.json", json.dumps(summary, indent=1, ensure_ascii=False))
    d = summary["describe_toolset"]
    print(f"\ntools/list: {summary['tools_list']['count']} tools, ~{summary['tools_list']['est_tokens']} tokens")
    if "list_toolsets" in summary:
        print(f"list_toolsets: ~{summary['list_toolsets']['est_tokens']} tokens")
    print(f"describe_toolset (all {d['toolsets']}): ~{d['total_est_tokens']} tokens; "
          f"compact catalog: ~{d['catalog_total_est_tokens']} tokens ({d['compression']}x smaller)")
    print(f"\nShare {os.path.join(out_dir, 'summary.json')} (numbers only). Keep raw/ and catalog.txt local.")


def _write(directory, filename, text):
    with open(os.path.join(directory, filename), "w", encoding="utf-8") as fh:
        fh.write(text)


if __name__ == "__main__":
    main()
