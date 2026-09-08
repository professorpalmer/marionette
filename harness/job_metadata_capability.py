"""Optional bounded metadata API; legacy runtime startup must not depend on it."""
from dataclasses import dataclass
from importlib import import_module
from importlib.util import find_spec
from inspect import signature


@dataclass(frozen=True)
class ActiveContext:
    session_id: str
    repo: str
    generation: str


class ViewChanged(RuntimeError):
    pass


UNAVAILABLE_REASON = "bounded_metadata_unsupported"
STORE_METHODS = (
    "list_job_summaries", "list_task_refs", "list_artifact_refs",
    "get_selected_economics", "historical_evidence_counts", "list_attempt_refs",
    "list_run_refs", "list_process_outcome_refs", "list_usage_observation_refs",
    "get_completion_receipt",
)


def bounded_metadata_available():
    """Check public contracts without opening stores or masking import failures."""
    modules = ("identity", "models", "state", "store_factory", "store", "sqlite_store")
    if any(find_spec("puppetmaster." + name) is None for name in modules):
        return False
    identity, models, state, factory, file_store, sqlite_store = (
        import_module("puppetmaster." + name) for name in modules
    )
    ref = getattr(models, "JobRef", None)
    create = getattr(factory, "create_store", None)
    if (not hasattr(identity, "StoreIdentityError") or not callable(ref)
            or not callable(getattr(state, "state_identity", None)) or not callable(create)):
        return False
    if (not {"version", "incarnation"} <= signature(ref).parameters.keys()
            or "mode" not in signature(create).parameters):
        return False
    return all(callable(getattr(store, method, None))
               for store in (getattr(file_store, "SwarmStore", None),
                             getattr(sqlite_store, "SQLiteSwarmStore", None))
               for method in STORE_METHODS)


def create_metadata_reader(capture, sources=None, local_handle=None):
    """Only import the new reader after the host has established capability."""
    from .job_readmodel import KnownSources, MetadataReader
    return MetadataReader(capture, sources if sources is not None else KnownSources(()), local_handle)


def metadata_handler(name):
    """Adapt an optional metadata endpoint to the registry-owned view."""
    def handle(request, view):
        if not view.supported:
            return 503, dict(code="metadata_unavailable", availability="unavailable",
                             missing=[UNAVAILABLE_REASON])
        from .api import job_readmodel
        return getattr(job_readmodel, name)(request, view.reader())
    return handle
