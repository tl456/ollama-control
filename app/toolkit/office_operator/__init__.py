"""Unified Office tool; all models receive the same functionality."""
def execute(args,scope=None):
    from ...office_operations import execute as run
    return run(args,scope)

run=execute
