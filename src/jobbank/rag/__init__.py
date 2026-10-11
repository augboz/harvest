"""Retrieval layer: semantic + keyword search over every record the pipeline
has ever seen, and the pieces an agent needs to answer questions from them.

Optional, like the MCP server: it needs the `rag` extra, and nothing in the
daily pipeline imports it. Each module is small and testable offline:

  corpus    state.db (read-only) + any captured full text  ->  Record list
  snapshot  one-off fetch-only pass that captures full posting text
  embed     Embedder protocol: a deterministic offline HashEmbedder for tests,
            and GeminiEmbedder (gemini-embedding-2 on Google Cloud's Agent
            Platform, formerly Vertex AI) with a resumable on-disk cache
  index     builds the LanceDB table: vectors + a BM25 full-text index
  search    bm25 / vector / hybrid, the last fused with our own RRF so the
            per-leg ranks and the raw cosine stay visible to the caller

The index lives per profile at <profile root>/rag_index/ (gitignored). It must
never go in state.db, which the daily run commits back to git.
"""
