# Alem.ai API — T1.4 verification

## Provider documentation

Alem Plus documents its OpenAI-compatible API at `https://llm.alem.ai/v1`;
its chat completion route is `/v1/chat/completions`. The site also
describes its LLM catalog as OpenAI-compatible. The configured default
model identifier in this project is `qwen3-8`; the exact model name and
native function-calling behavior are account/deployment dependent and
must be established by the authenticated probe below.

Sources: [Alem Plus Gemma 4 API example](https://doc.alem.ai/services/gemma4.html),
[Alem Plus GPT OSS API example](https://doc.alem.ai/services/gptoos.html),
[Alem Plus service catalog](https://plus.alem.ai/services).

## Authenticated probe

- Timestamp (UTC): `2026-10-06T07:28:12+00:00`
- Base URL: `https://llm.alem.ai/v1`
- Model: `gemma4`
- plain completion: **works** — completion returned
- native tools: **works** — echo tool call returned
- JSON protocol: **works** — echo tool call returned

Conclusion for this configured model: native tools worked; the JSON protocol worked. Authentication/permission failures do not establish whether a protocol is supported. Set `LLM_TOOL_MODE=auto` to prefer native function calling and fall back to the JSON protocol when the server rejects it, or select `native` / `json` explicitly.
