# eval/

`qa.yaml` is the retrieval test set: real-style questions plus the document(s) that answer each
one. `rag-drg eval` runs them through search and reports hit@1, hit@6 and MRR overall and per tag.

```bash
rag-drg eval --show-failures           # full index (after `rag-drg ingest --fetch`)
rag-drg eval --tags paraphrase         # questions that share few words with their answer
rag-drg queries to-qa --since 30d      # candidate questions from the MCP query log
```

Format, how to read the metrics, how to compare keyword search with embeddings and how the
query log feeds new questions: [`docs/evaluation.md`](../docs/evaluation.md).

Rules: write questions the way people ask them, verify each expected document really contains
the answer, and fix failures by improving the knowledge base, not by rewording the question.
