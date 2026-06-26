"""Worker leaf-toolset: a no-delegation worker must NOT see run_implement/run_parallel/
run_swarm (it edits directly), while the main pilot still advertises them."""
from harness.pilot import build_tools_schema


def _names(schema):
    return {t["function"]["name"] for t in schema}


def test_leaf_excludes_delegation_tools():
    leaf = _names(build_tools_schema(no_delegation=True))
    assert "run_implement" not in leaf
    assert "run_parallel" not in leaf
    assert "run_swarm" not in leaf
    # direct-edit tools must remain
    assert "write_file" in leaf
    assert "read_file" in leaf
    assert "run_command" in leaf


def test_main_pilot_keeps_delegation_tools():
    # regression guard: the interactive pilot can still delegate
    main = _names(build_tools_schema())
    assert "run_implement" in main
    assert "run_parallel" in main


def test_provider_worker_sets_no_delegation():
    # ProviderWorker must construct its session as a no-delegation leaf
    import inspect
    import harness.worker as wk
    src = inspect.getsource(wk)
    assert "no_delegation=True" in src
