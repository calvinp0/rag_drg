<!-- Codex custom prompt: copy to ~/.codex/prompts/lesson.md. Same as integrations/claude-code/commands/lesson.md. -->
Record a lesson in the group's knowledge base (rag-drg) from this conversation. Note from me, if any: $ARGUMENTS

1. Find the correction in this conversation: what you, a tool or the knowledge base got wrong,
   and what is right.
2. Draft the lesson:
   - `title`: one line that states the rule itself;
   - `mistake`, and `correction` with the concrete keywords, paths or values;
   - `domain` (ess, arc, hpc, project or literature), `software`, and `version` if it matters;
   - `evidence`: where it was confirmed, e.g. a job output, a manual section, or my statement;
   - `tags`.
3. Write only what this conversation or a source established; mark anything unverified as "untested".
   Keep it general: describe the rule, not someone's home directory or username.
4. Show me the draft and wait for my OK. Then call the rag-drg MCP tool `record_lesson`. Report the
   file it wrote and any similar lessons it flagged. If one of those already covers the point, say so
   instead of adding a duplicate.

If the rag-drg MCP server is not connected, run
`rag-drg lesson --title ... --mistake ... --correction ... --domain ... [--software ...]` instead.
