"""rag_drg: a shared knowledge base for the research group's coding agents.

Covers electronic structure software (ORCA, Gaussian, Psi4, Molpro, PySCF),
the ARC codebase, HPC cluster usage and project literature. Content is
chunked into a SQLite index (FTS5 keyword search + optional dense vectors)
and served to agents through an MCP server or the ``rag-drg`` CLI.
"""

__version__ = "0.1.0"
