# Nikon Expert — Field Knowledge System for Lithography Tools

[简体中文](README.md) | **English**

> A knowledge system for field engineers working on Nikon NSR scanners (mainly S207D / S307E, also S205C / S206D / S308F / SF series):
> troubleshoot alarms, look up error codes, find step-by-step procedures, trace wiring — every answer cites its source.
> Documents come from WPS, Feishu and local files, and are synced and distilled automatically.

Updated 2026-10-17. The Chinese README is the primary version; design history is in [CLAUDE.md](CLAUDE.md), plans in [ROADMAP.md](ROADMAP.md).

## Features

| Entry | Purpose |
|---|---|
| **Chat** | Ask anything. Simple questions use fast retrieval; troubleshooting and "how do I…" questions go to a **multi-step ReAct agent** that decides which sources to query, cites every step |
| **SOP lookup** | Complete procedures from manuals, check sheets and in-house SOPs, in original order; one click rewrites one as a Chinese SOP |
| **Error codes** | Exact lookup; field notation like `STG-29-80` is converted to the manual's main / detail code |
| **Troubleshooting table** | Experience by model → unit: OEM countermeasures, fixes verified in the field, occurrence counts, dead ends. Distilled entries stay *pending* until an admin confirms them |
| **Work logs** | Live view of each site's commissioning log on Feishu |
| **Claude (MCP)** | 13 tools for querying the knowledge base, viewing circuit diagrams and tracing wires |

Scale (2026-10-17): 700 documents, 229k vectors, 3,709 procedures, 219 abbreviations, 2,375 troubleshooting entries, 8,324 wiring connections (S207D).

## Pipeline

```
Sources                 Ingestion (automatic)                 Indexes / distilled data            Use
WPS   ─every 30 min─┐                                      ┌→ full-text index (FTS5)
Feishu ─every 30 min┼→ parse → detect model → set ACL ───→ ├→ vector store (bge-m3)          ┌→ web UI
local files ─manual─┘   (PDF/Word/Excel/md, OCR,           ├→ SOP catalogue                ──→ ├→ Claude (MCP)
                         circuit-diagram recognition)       ├→ abbreviation glossary          └→ ReAct agent
                                                            ├→ diagram index + wiring table
                                                            └→ troubleshooting table ← nightly distillation (local model on the user's Mac)
```

- **Sources**: self-written, continuously updated notes live in Feishu; manuals, PDFs, spreadsheets and diagrams live in WPS. Changed files replace old versions; deletions are synced.
- **Query**: abbreviations are expanded before retrieval (RR → Reticle Rotation); the named machine model is preferred; up to 3 questions are answered concurrently.
- **Nightly distillation** (`nikon-kb-extract.timer`, 00:00–06:30 Beijing time): runs only when the user's Mac is online and idle, using a local gemma4 model over Tailscale — documents never leave our own devices. Field logs → field cases; manuals / Japanese check sheets → OEM entries; SOPs → classified by unit. Identical content is processed once; work logs are tracked per day, so only new or edited days are processed.

## Cloud model policy (confidentiality)

| Step | Model | Note |
|---|---|---|
| Interactive Q&A, rewriting one SOP | deepseek-v4-flash (cloud) | only a few retrieved passages per question; background "thinking" off by default |
| Embeddings | SiliconFlow bge-m3 (cloud, free) | terms state no training, data destroyed after inference |
| **Bulk distillation / extraction** | **gemma4 on the user's Mac (local)** | never sent to the cloud; stops if the Mac is unavailable |
| Anthropic API | not used | not available to China-controlled companies |

## Deployment

Server: Vultr, AlmaLinux 8, 2 cores / 3.6 GB, CPU only, project at `/opt/nikon-expert`. Services: `nikon-expert` (Gradio web UI, port 7860, per-user login + ACL), `nikon-mcp`; timers for WPS / Feishu sync, Feishu token refresh, circuit recognition and nightly distillation. Storage: Qdrant, `data/fts.db`, `data/sop_index.db`, `data/ts_table.db`, `data/netlist.db`, `data/glossary.json`.

Workflow: change code on branch `feat/feishu-worklog`, commit and push, copy to the server, then open a PR into `main`. Never edit code directly on the server; restart services at night (02:00–05:00).
