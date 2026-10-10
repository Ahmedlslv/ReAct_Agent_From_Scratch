# agent package
# Exposes the two agent entry points at the package level so callers can write:
#   from agent import run_react
#   from agent import run_react_reflective
# instead of the longer:
#   from agent.base import run_react
#   from agent.reflective import run_react_reflective

__all__ = ["run_react", "run_react_reflective"]


def __getattr__(name):
    # Load only the requested agent. The original reflection module still uses
    # importing the Gemini base agent.
    if name == "run_react":
        from agent.base import run_react
        return run_react
    if name == "run_react_reflective":
        from agent.reflective import run_react_reflective
        return run_react_reflective
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
