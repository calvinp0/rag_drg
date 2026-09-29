"""Agent-task evaluation (rag-drg agent-eval): does an agent do real group tasks correctly, with
and without rag-drg? See docs/agent-eval.md.

    tasks.py    load / validate eval/tasks.yaml
    graders.py  deterministic pass/fail checks on the files an agent wrote and its final answer
    runner.py   run an agent command per task x condition x repeat, grade, report
"""
