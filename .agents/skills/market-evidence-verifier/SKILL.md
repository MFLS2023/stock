---
name: market-evidence-verifier
description: Verify current or date-sensitive A-share claims with external tools, official disclosures, market data, and deterministic calculations while separating them from historical local-corpus opinions. Use when questions contain today, current, latest, still valid, announcement, price, sector status, financial figures, or any claim that may have changed since a source document was created.
---

# Market Evidence Verifier

## Workflow

1. Extract the exact claim, ticker/entity, market, and required cutoff time.
2. Treat local knowledge-base material as historical method or opinion, not current proof.
3. Prefer official and primary evidence: exchange/company disclosures, periodic reports, investor-relations records, then reputable market-data providers.
4. Use MCP, Web, or an API for changing data. State the evidence timestamp and source URL.
5. Calculate prices, returns, ratios, drawdowns, or comparisons with code or structured data.
6. Present separate sections for:
   - historical corpus view;
   - current verified evidence;
   - agreement, conflict, or uncertainty;
   - what would invalidate the conclusion.
7. If live evidence is unavailable, say that current status could not be verified.

Do not execute trades, promise returns, or transform a historical quote into a current buy/sell instruction.
