# CLAUDE.md

## Python Environment

- Always run Python through a project-local virtual environment; never install packages into the system Python.
- Environment location depends on the platform:
  - Windows: `venv/` (create with `python -m venv venv`, use `venv\Scripts\python.exe`)
  - WSL / Linux: `venv-wsl/` (create with `python3 -m venv venv-wsl`, use `venv-wsl/bin/python`)
- If the environment for the current platform does not exist yet, create it first and install dependencies (`numpy`, `shapely`, `numba`, `gurobipy`) before running solvers or evaluation scripts.
- Do NOT put `.codex_deps/` on `PYTHONPATH` (its binaries are cp312-only and break other interpreters).

## Response Language

- Respond to the user in Korean.
- Technical terms, code identifiers, filenames, commands, library names, and AI-tooling terms such as rule, `CLAUDE.md`, skill, and agent may remain in English when that is clearer.
- Documents written for AI tools or agent instructions may be written in English.
