---
name: trading-knowledge-tutor
description: Teach trading concepts from the user's local multi-source corpus with traceable evidence, beginner-friendly explanations, prerequisites, failure cases, counterexamples, and practice prompts. Use when the user asks how a method works, what a trader meant, how to learn or review a concept, or requests an evidence-backed lesson from one or more source libraries.
---

# Trading Knowledge Tutor

## Workflow

1. Identify the requested source scope. If unspecified, search all integrated sources but label each source separately.
2. Query `_知识库系统/indexes/knowledge.db` with `_知识库系统/scripts/query_kb.py`.
3. Read the full matched chunk and its parent context before teaching.
4. Explain in this order:
   - plain-language definition;
   - evidence from the corpus;
   - required market conditions;
   - observable signals;
   - invalidation and common misuse;
   - a short exercise or review checklist.
5. Cite as `[来源｜作者或嘉宾｜文档｜日期｜定位]`.
6. Distinguish historical opinions from verifiable facts. Use `$market-evidence-verifier` for current applicability.
7. If the requested source is not yet integrated or no evidence is found, say so directly and do not reconstruct the author's view from general knowledge.
8. After explaining a concept, offer to generate a method card draft. If the user confirms, write to `_知识库系统/methods.jsonl` with these fields:
   - `core_claim`: one-sentence executable statement
   - `conditions`: required market setup
   - `invalidation`: when the method fails
   - `source_quote`: verbatim excerpt with locator
   - `status`: always `draft` until the user explicitly approves
   - `created`: today's date

Avoid deterministic trade instructions and return calculations only when produced from source data or code.
