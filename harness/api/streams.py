"""SSE stream route bodies (peeled from ``harness.server.Handler``).

``stream_run`` / ``stream_auto`` / ``stream_chat`` take a handler-like object
(``send_response`` / ``wfile`` / ``_send`` / ``_cors``) plus
:class:`StreamServices` so this module never imports ``harness.server`` at
top level. ``server.Handler`` keeps thin wrappers that inject live globals.
"""

from __future__ import annotations

from ..prompt_queue import ORIGINAL_TEXT_UNSET
from ..pilot_replacement import input_admission, input_projection

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from harness.diag import note as _diag_note

from .sse import StreamEventDict, _sse_ring_begin, sse_pump, sse_write

# Event kinds that mean a tool result / action completion has just been appended
# to _history -- checkpoint immediately (ignoring throttle) when we see one so a
# crash right after an action never loses that appended chunk of the transcript.
# Wave 4: action_result also covers background command pending receipts (job_id)
# so launch identity survives a crash before the child process settles.
CHECKPOINT_KINDS = frozenset({
    "action_result",
    "swarm_result",
    # Terminal kinds: a crash in the 2s throttle window must not lose the
    # final assistant text / error / interrupt receipt.
    "assistant_done",
    "error",
    "interrupted",
    "auto_halt",
    "final",
})


def should_force_transcript_checkpoint(ev: Any) -> bool:
    """True when the transcript must flush immediately (Wave 4 reattach).

    Command-job pending/terminal ``action_result`` frames carry durable
    ``job_id`` identity; losing them across a crash breaks restart recovery.
    Background ``action_start`` frames with a job_id are treated the same.
    """
    kind = getattr(ev, "kind", None) or ""
    if kind in CHECKPOINT_KINDS:
        return True
    if kind != "action_start":
        return False
    data = getattr(ev, "data", None) or {}
    if not isinstance(data, dict):
        return False
    if data.get("job_id"):
        return True
    mode = str(data.get("mode") or "").strip().lower()
    return mode == "background"


def _encode_run_sse_frame(ev: Any) -> bytes:
    """SessionEvent /run frame: includes ``turn`` (chat frames omit it)."""
    frame: StreamEventDict = {"kind": ev.kind, "turn": ev.turn, "data": ev.data}
    return f"data: {json.dumps(frame)}\n\n".encode()


def _encode_chat_sse_frame(ev: Any) -> bytes:
    """ConvEvent chat/auto frame: kind + data only (no ``turn``)."""
    frame: StreamEventDict = {"kind": ev.kind, "data": ev.data}
    return f"data: {json.dumps(frame)}\n\n".encode()


@dataclass
class StreamServices:
    """Explicit deps for SSE stream handlers (injected by ``server.py``)."""

    cfg: Any
    sessions: Any
    get_pilot: Callable[[], Any]
    get_session: Callable[[], Any]
    ensure_pilot_matches_driver: Callable[[], Any]
    maybe_refresh_codegraph: Callable[[str], None]
    pilot_preflight: Callable[[], Any]
    checkpoint_transcript: Callable[..., None]
    finalize_turn: Callable[[Any], None]
    upload_dir: str
    # Call-time lookup so tests can patch harness.server.AutoBudget.
    auto_budget_from_env: Callable[[], Any]
    pilot_swap_lock: Any = None
    get_runners: Optional[Callable[[], Any]] = None
    ensure_session_driver: Optional[Callable[[str], Any]] = None


def validate_upload_image_paths(
    raw_images: str, upload_dir: str, *, session=None
) -> tuple[Optional[list], Optional[tuple[int, dict]]]:
    """Validate pipe-separated image paths are under ``upload_dir``.

    Used by ``GET /api/run`` and ``GET /api/chat`` query parsing. Returns
    ``(paths, None)`` on success or ``(None, (status, payload))`` on error.
    """
    from .session_control import _validate_upload_images
    return _validate_upload_images([p for p in (raw_images or '').split('|') if p], upload_dir, session=session)


def resolve_stashed_chat_message(
    mid: str,
    message: str,
    raw_images: str,
    stash_pop: Callable[[str], Optional[dict]],
) -> tuple[str, str]:
    """Apply a stashed ``mid`` onto message/images for ``GET /api/chat``.

    Unknown/expired mid falls through with whatever query-string values remain.
    """
    if not mid:
        return message, raw_images
    stashed = stash_pop(mid)
    if stashed is not None:
        message = stashed.get("message", "")
        stashed_images = stashed.get("images") or []
        if stashed_images and not raw_images:
            raw_images = "|".join(stashed_images)
    return message, raw_images


def stream_run(handler: Any, prompt: str, images, svc: StreamServices) -> Any:
    """Stream a classic Session.run turn over SSE."""
    try:
        svc.ensure_pilot_matches_driver()
    except Exception as e:
        return handler._send(500, json.dumps({"error": str(e)}))
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "keep-alive")
    handler._cors()
    handler.end_headers()

    if svc.sessions.active and prompt:
        from ..sessions import derive_title
        svc.sessions.set_title_if_default(svc.sessions.active, derive_title(prompt))

    if svc.cfg.repo and os.path.isdir(svc.cfg.repo):
        svc.maybe_refresh_codegraph(svc.cfg.repo)

    session = svc.get_session()
    pre = session.preflight()
    if pre:
        handler.wfile.write(
            f"data: {json.dumps({'kind':'error','turn':0,'data':{'error':pre}})}\n\n".encode()
        )
        handler.wfile.write(b"data: {\"kind\": \"done\"}\n\n")
        handler.wfile.flush()
        return

    from ..hooks import run_hooks
    # Bind turn identity before any view switch can reassign globals.
    turn_pilot = svc.get_pilot()
    turn_sid = svc.sessions.active or getattr(turn_pilot, "harness_session_id", "") or ""
    ctx = {"session_id": turn_sid, "prompt": prompt, "pilot": turn_pilot}
    run_hooks("preRun", ctx)
    gen = session.run(prompt, images=images or None)
    ring = _sse_ring_begin(turn_sid)
    try:
        sse_pump(
            handler.wfile,
            gen,
            _encode_run_sse_frame,
            ring=ring,
        )
    finally:
        run_hooks("postRun", ctx)


def _stream_session_pilot(svc, session_id):
    from ..input_receipts import InputReceiptError
    from ..session_runners import resolve_session_runner
    if session_id is None:
        return svc.get_pilot()
    if svc.get_runners is None:
        # Compatibility for focused/embedded stream services that predate the
        # runner registry.  They may serve only their one current owner; a
        # mismatched explicit ID is still rejected rather than retargeted.
        pilot = svc.get_pilot()
    else:
        pilot = resolve_session_runner(svc.get_runners(), session_id)
    if session_id is not None and (
        not isinstance(session_id, str) or not session_id
        or getattr(pilot, 'harness_session_id', None) != session_id
    ):
        raise InputReceiptError('input_session_changed', 'The input owner changed. Your input was not admitted; keep your draft.')
    return pilot


def _stream_turn_config(svc, pilot, session_id):
    """Return the immutable turn owner's config.

    The active runner follows live workspace/settings changes held by the
    service config.  A background runner must retain its own config so a view
    switch cannot redirect mentions, CodeGraph, hooks, or persistence.
    """
    if (session_id is None
            or (getattr(svc.sessions, "active", None) == session_id
                and svc.get_pilot() is pilot)):
        return svc.cfg
    return getattr(pilot, "config", svc.cfg)


@input_projection
def _admit_stream_input(pilot, text, images, upload_dir, *, documents=None,
                        retry_key=None, input_id=None, handoff_token=None, original_text=ORIGINAL_TEXT_UNSET):
    from ..prompt_queue import PromptQueueMixin
    from ..input_receipts import session_input_store, InputReceiptError
    if original_text is not ORIGINAL_TEXT_UNSET and not isinstance(original_text, str):
        raise InputReceiptError('input_invalid', 'Original text must be a string.')
    if not isinstance(pilot, PromptQueueMixin):
        return None, {}, text, images
    if not getattr(pilot, 'harness_session_id', ''):
        raise InputReceiptError('input_owner_required', 'Open a workspace or pick a project session before submitting input.')
    pilot._input_upload_root = upload_dir
    store = session_input_store(pilot)
    if input_id:
        try:
            receipt = store.get(input_id)
        except InputReceiptError as exc:
            # Client-allocated optimistic input_id on first send: create, do not
            # treat as a missing handoff target.
            if exc.code != 'input_unknown' or handoff_token:
                raise
            receipt = store.admit(
                text, original_text=original_text, images=images, documents=documents,
                upload_root=upload_dir, retry_key=retry_key, input_id=input_id)
            if receipt['status'] != 'accepted' or receipt['owner_instance'] != store.instance:
                raise InputReceiptError('input_held', 'Input is held or already attempted; inspect its receipt.')
            input_id = receipt['id']
        else:
            accepted = receipt['status'] == 'accepted' and not handoff_token
            handed_off = (receipt['status'] == 'delivering' and handoff_token
                          and receipt.get('handoff_token') == handoff_token and not receipt.get('handoff_claimed'))
            if receipt['owner_instance'] != store.instance or not (accepted or handed_off):
                raise InputReceiptError('input_handoff_conflict', 'Input is held or already attempted; inspect its receipt.')
            text = receipt.get('delivery_text', receipt['original_text'])
    else:
        if handoff_token:
            raise InputReceiptError('input_handoff_conflict', 'A handoff requires its input ID.')
        receipt = store.admit(text, original_text=original_text, images=images, documents=documents, upload_root=upload_dir, retry_key=retry_key)
        if receipt['status'] != 'accepted' or receipt['owner_instance'] != store.instance:
            raise InputReceiptError('input_held', 'Input is held or already attempted; inspect its receipt.')
        input_id = receipt['id']
    images = [a['ref'] for a in receipt['attachments'] if a['kind'] == 'image']
    return receipt, {'input_id': input_id, 'handoff_token': handoff_token}, text, images


def _admit_owned_stream_input(svc, pilot, session_id, *args, **kwargs):
    from ..input_receipts import InputReceiptError
    sid = getattr(pilot, 'harness_session_id', '')
    def validate():
        if (_stream_session_pilot(svc, session_id) is not pilot
                or getattr(pilot, 'harness_session_id', '') != sid
                or (sid and svc.get_runners is not None and svc.get_runners().get(sid) is not pilot)):
            raise InputReceiptError('input_session_changed', 'The input owner changed; keep your draft.')
    with input_admission(pilot, svc.pilot_swap_lock, validate):
        return _admit_stream_input(pilot, *args, **kwargs)


def stream_auto(handler: Any, objective: str, svc: StreamServices, images=None, *, documents=None, retry_key=None, input_id=None, handoff_token=None, session_id=None, original_text=ORIGINAL_TEXT_UNSET) -> Any:
    """Stream the fully-auto loop (governor-bounded) over SSE."""
    from ..input_receipts import InputReceiptError
    try:
        _stream_session_pilot(svc, session_id)
        if session_id is None:
            svc.ensure_pilot_matches_driver()
        elif svc.ensure_session_driver is not None:
            svc.ensure_session_driver(session_id)
    except InputReceiptError as exc:
        return handler._send(409, json.dumps(exc.payload()))
    except Exception as e:
        return handler._send(500, json.dumps({"error": str(e)}))
    try:
        turn_pilot = _stream_session_pilot(svc, session_id)
        receipt, receipt_args, objective, images = _admit_owned_stream_input(
            svc, turn_pilot, session_id, objective, images, svc.upload_dir, documents=documents,
            retry_key=retry_key, input_id=input_id, handoff_token=handoff_token, original_text=original_text)
    except InputReceiptError as exc:
        return handler._send(409 if exc.code == "input_session_changed" else 503, json.dumps(exc.payload()))
    turn_sid = getattr(turn_pilot, "harness_session_id", "") or svc.sessions.active or ""
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler._cors()
    handler.end_headers()
    if receipt_args:
        sse_write(handler.wfile, ('data: ' + json.dumps({'kind': 'input_receipt', 'data': {'input_id': receipt['id'], 'status': receipt['status']}}) + '\n\n').encode())

    if turn_sid and objective:
        from ..sessions import derive_title
        svc.sessions.set_title_if_default(turn_sid, derive_title(objective))

    turn_config = _stream_turn_config(svc, turn_pilot, session_id)
    if turn_config.repo and os.path.isdir(turn_config.repo):
        svc.maybe_refresh_codegraph(turn_config.repo)

    from ..hooks import run_hooks
    # Bind turn identity before any view switch can reassign globals.
    ctx = {"session_id": turn_sid, "objective": objective, "pilot": turn_pilot, "config": turn_config, "repo": turn_config.repo}
    run_hooks("preRun", ctx)
    budget = svc.auto_budget_from_env()
    gen = turn_pilot.run_auto(objective, budget, images=images or None, **receipt_args)
    last_ckpt = time.monotonic()

    def _maybe_checkpoint(ev):
        nonlocal last_ckpt
        # Incremental checkpoint: flush immediately after an appended action
        # result (incl. command-job pending receipts), else on a 2s throttle,
        # so a crash mid governor-loop can't lose the last chunk of transcript
        # before _finalize_turn runs.
        if should_force_transcript_checkpoint(ev):
            svc.checkpoint_transcript(ctx)
            last_ckpt = time.monotonic()
        elif time.monotonic() - last_ckpt >= 2.0:
            svc.checkpoint_transcript(ctx)
            last_ckpt = time.monotonic()

    try:
        # Detach != cancel: closing the EventSource must not stop the
        # governor. Explicit Stop uses /api/session/interrupt -> cancel().
        ring = _sse_ring_begin(turn_sid)
        sse_pump(
            handler.wfile,
            gen,
            _encode_chat_sse_frame,
            on_event=_maybe_checkpoint,
            ring=ring,
        )
    finally:
        svc.finalize_turn(ctx)


def stream_chat(
    handler: Any,
    message: str,
    images,
    svc: StreamServices,
    *,
    plan: bool = False,
    resume: bool = False,
    input_id=None,
    handoff_token=None,
    documents=None,
    retry_key=None,
    session_id=None,
    original_text=ORIGINAL_TEXT_UNSET,
) -> Any:
    """Stream the conversational PILOT loop: prose messages + collapsible
    action cards (run_swarm) + assistant_done.

    ``resume=True`` runs a keep-alive continuation turn: no new user message
    is appended -- the pilot generates off the history that drain_swarm_results
    already extended with the finished job's result + continuation."""
    from ..input_receipts import InputReceiptError
    try:
        _stream_session_pilot(svc, session_id)
        if session_id is None:
            svc.ensure_pilot_matches_driver()
        elif svc.ensure_session_driver is not None:
            svc.ensure_session_driver(session_id)
        turn_pilot = _stream_session_pilot(svc, session_id)
    except InputReceiptError as exc:
        return handler._send(409, json.dumps(exc.payload()))
    except Exception as e:
        return handler._send(500, json.dumps({"error": str(e)}))
    receipt_args = {}
    if not resume:
        from ..input_receipts import InputReceiptError
        try:
            receipt, receipt_args, message, images = _admit_owned_stream_input(
                svc, turn_pilot, session_id, message, images, svc.upload_dir, documents=documents,
                retry_key=retry_key, input_id=input_id, handoff_token=handoff_token, original_text=original_text)
            input_id = receipt_args.get('input_id')
        except InputReceiptError as exc:
            return handler._send(409 if exc.code == "input_session_changed" else 503, json.dumps(exc.payload()))
    turn_sid = getattr(turn_pilot, "harness_session_id", "") or svc.sessions.active or ""
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "keep-alive")
    handler._cors()
    handler.end_headers()
    if receipt_args:
        sse_write(handler.wfile, ('data: ' + json.dumps({'kind': 'input_receipt', 'data': {'input_id': input_id, 'status': receipt['status']}}) + '\n\n').encode())

    if turn_sid and message:
        from ..sessions import derive_title
        svc.sessions.set_title_if_default(turn_sid, derive_title(message))

    # Self-healing CodeGraph: debounced staleness check at the start of every
    # turn, so an index that drifted (files edited/added/DELETED since the last
    # build) reindexes in the background before it misleads the pilot. The
    # debounce in _maybe_refresh_codegraph prevents thrash during rapid turns.
    turn_config = _stream_turn_config(svc, turn_pilot, session_id)
    if turn_config.repo and os.path.isdir(turn_config.repo):
        svc.maybe_refresh_codegraph(turn_config.repo)

    # Resolve @-file, @folder, @symbol, and @codebase mentions in message
    resolved_files = []
    resolved_folders = []
    resolved_symbols = []
    resolved_codebases = []
    total_size = 0
    repo = turn_config.repo
    if repo and os.path.isdir(repo) and message:
        from ..mention_context import (
            MENTION_TOTAL_BUDGET,
            SYMBOL_SNIPPET_CAP,
            expand_codebase_mention,
            expand_folder_mention,
            extract_mention_tokens,
            format_codebase_mention_skip,
            format_file_mention_skip,
            format_folder_mention_skip,
            format_symbol_mention_block,
            format_symbol_mention_failure,
            format_symbol_mention_skip,
            is_codebase_mention,
            read_file_mention,
            resolve_repo_dir,
        )

        tokens = extract_mention_tokens(message)
        seen_tokens = set()
        for token in tokens:
            if receipt_args and os.path.isabs(token):
                try:
                    if os.path.commonpath([os.path.realpath(svc.upload_dir), os.path.realpath(token)]) == os.path.realpath(svc.upload_dir):
                        # The native receipt supplies these bytes at delivery.
                        continue
                except ValueError:
                    pass
            if token in seen_tokens:
                continue
            seen_tokens.add(token)

            # Honest @codebase / @codebase:query — never fall through to
            # bare-token symbol search (Cursor migrants expect pinned CG).
            if is_codebase_mention(token):
                if total_size >= MENTION_TOTAL_BUDGET:
                    resolved_codebases.append(
                        format_codebase_mention_skip(
                            token,
                            reason=(
                                "mention context budget exhausted "
                                "(150KB total across @-mentions)"
                            ),
                        )
                    )
                    continue
                block = expand_codebase_mention(
                    repo, token, task_fallback=message,
                )
                read_size = len(block.encode("utf-8"))
                if (
                    "--- Codebase:" in block
                    and "... skipped:" not in block
                    and "... failed to resolve:" not in block
                    and total_size + read_size > MENTION_TOTAL_BUDGET
                ):
                    resolved_codebases.append(
                        format_codebase_mention_skip(
                            token,
                            reason=(
                                "mention context budget exhausted "
                                "(150KB total across @-mentions)"
                            ),
                        )
                    )
                else:
                    resolved_codebases.append(block)
                    # Skip/failure notes do not consume the shared budget.
                    if (
                        "... skipped:" not in block
                        and "... failed to resolve:" not in block
                    ):
                        total_size += read_size
                continue

            is_folder_prefix = token.startswith("folder:")
            is_symbol_prefix = token.startswith("symbol:")
            symbol_name = token[7:] if is_symbol_prefix else token

            # Honest @folder:path — or a bare path that is a workspace directory.
            if is_folder_prefix or (
                not is_symbol_prefix and resolve_repo_dir(repo, token) is not None
            ):
                if total_size >= MENTION_TOTAL_BUDGET:
                    resolved_folders.append(
                        format_folder_mention_skip(
                            token,
                            reason=(
                                "mention context budget exhausted "
                                "(150KB total across @-mentions)"
                            ),
                        )
                    )
                    continue
                block = expand_folder_mention(repo, token)
                if not block:
                    resolved_folders.append(
                        format_folder_mention_skip(
                            token,
                            reason="not found in workspace",
                        )
                    )
                    continue
                read_size = len(block.encode("utf-8"))
                if total_size + read_size > MENTION_TOTAL_BUDGET:
                    resolved_folders.append(
                        format_folder_mention_skip(
                            token,
                            reason=(
                                "mention context budget exhausted "
                                "(150KB total across @-mentions)"
                            ),
                        )
                    )
                else:
                    resolved_folders.append(block)
                    total_size += read_size
                continue

            is_file = False
            file_to_read = None
            if not is_symbol_prefix:
                full_path = os.path.abspath(os.path.join(repo, token))
                repo_real = os.path.realpath(repo)
                full_real = os.path.realpath(full_path)
                try:
                    common = os.path.commonpath([repo_real, full_real])
                    if common == repo_real and os.path.isfile(full_real):
                        is_file = True
                        file_to_read = full_real
                except Exception:
                    pass
                # Also accept files dropped from OUTSIDE the workspace: the
                # composer uploads those into the trusted upload dir and
                # references them by absolute path. Allow reading that path
                # too (drag-and-drop of external files).
                if not is_file:
                    try:
                        upload_real = os.path.realpath(svc.upload_dir)
                        abs_token = os.path.realpath(os.path.abspath(token))
                        if (os.path.commonpath([upload_real, abs_token]) == upload_real
                                and os.path.isfile(abs_token)):
                            is_file = True
                            file_to_read = abs_token
                    except Exception:
                        pass

            if is_file and file_to_read:
                block, added = read_file_mention(
                    file_to_read,
                    token,
                    total_size=total_size,
                    state_dir=getattr(turn_pilot, "state_dir", None),
                )
                resolved_files.append(block)
                total_size += added
                if added > 0:
                    note = getattr(turn_pilot, "note_working_path", None)
                    if callable(note):
                        note(token, edited=False)
            elif not is_symbol_prefix and ("/" in token or "\\" in token):
                # Path-like bare token that is not an existing workspace file —
                # do not fall through into symbol search as if it were a name.
                resolved_files.append(
                    format_file_mention_skip(
                        token,
                        reason="not found in workspace",
                    )
                )
            else:
                # @symbol / bare unknown — always emit skip/failure honesty;
                # never leave the token silent on CodeGraph miss/unavailable.
                try:
                    import puppetmaster.codegraph as cg
                    if not cg.codegraph_available():
                        resolved_symbols.append(
                            format_symbol_mention_skip(
                                symbol_name,
                                reason="CodeGraph unavailable",
                            )
                        )
                        continue
                    if not cg.codegraph_ready(repo):
                        resolved_symbols.append(
                            format_symbol_mention_skip(
                                symbol_name,
                                reason=(
                                    "CodeGraph index not ready "
                                    "(run codegraph init / wait for indexing)"
                                ),
                            )
                        )
                        continue
                    res = cg.codegraph_query(search=symbol_name, cwd=repo, limit=1)
                    if not (res.get("ok") and res.get("stdout")):
                        resolved_symbols.append(
                            format_symbol_mention_skip(
                                symbol_name,
                                reason="no matching symbol in CodeGraph",
                            )
                        )
                        continue
                    data = json.loads(res["stdout"])
                    if not (isinstance(data, list) and data):
                        resolved_symbols.append(
                            format_symbol_mention_skip(
                                symbol_name,
                                reason="no matching symbol in CodeGraph",
                            )
                        )
                        continue
                    node = data[0].get("node")
                    if not node:
                        resolved_symbols.append(
                            format_symbol_mention_skip(
                                symbol_name,
                                reason="no matching symbol in CodeGraph",
                            )
                        )
                        continue
                    file_path = node.get("filePath")
                    start_line = node.get("startLine")
                    end_line = node.get("endLine")
                    name = node.get("name") or symbol_name
                    if not file_path or start_line is None:
                        resolved_symbols.append(
                            format_symbol_mention_skip(
                                name,
                                reason="no matching symbol in CodeGraph",
                            )
                        )
                        continue
                    if total_size >= MENTION_TOTAL_BUDGET:
                        resolved_symbols.append(
                            format_symbol_mention_skip(
                                name,
                                reason=(
                                    "mention context budget exhausted "
                                    "(150KB total across @-mentions)"
                                ),
                            )
                        )
                        continue
                    sym_full_path = os.path.abspath(os.path.join(repo, file_path))
                    repo_real = os.path.realpath(repo)
                    sym_full_real = os.path.realpath(sym_full_path)
                    common = os.path.commonpath([repo_real, sym_full_real])
                    if common != repo_real or not os.path.isfile(sym_full_real):
                        resolved_symbols.append(
                            format_symbol_mention_skip(
                                name,
                                reason="symbol file not found in workspace",
                            )
                        )
                        continue
                    try:
                        with open(sym_full_real, "r", encoding="utf-8", errors="replace") as f:
                            lines = f.readlines()

                        start_idx = max(0, int(start_line) - 1)
                        if end_line is not None:
                            end_idx = min(len(lines), int(end_line))
                        else:
                            end_idx = min(len(lines), start_idx + 60)

                        snippet_lines = lines[start_idx:end_idx]
                        snippet = "".join(snippet_lines)
                        original_bytes = len(snippet.encode("utf-8"))
                        truncated = original_bytes > SYMBOL_SNIPPET_CAP
                        if truncated:
                            snippet = snippet.encode("utf-8")[:SYMBOL_SNIPPET_CAP].decode(
                                "utf-8", errors="ignore"
                            )

                        read_size = len(snippet.encode("utf-8"))
                        if total_size + read_size > MENTION_TOTAL_BUDGET:
                            resolved_symbols.append(
                                format_symbol_mention_skip(
                                    name,
                                    reason=(
                                        "mention context budget exhausted "
                                        "(150KB total across @-mentions)"
                                    ),
                                )
                            )
                        else:
                            resolved_symbols.append(
                                format_symbol_mention_block(
                                    name,
                                    file_path,
                                    int(start_line),
                                    snippet,
                                    truncated=truncated,
                                    original_bytes=original_bytes,
                                )
                            )
                            total_size += read_size
                    except OSError as exc:
                        resolved_symbols.append(
                            format_symbol_mention_failure(
                                name,
                                error=str(exc) or type(exc).__name__,
                            )
                        )
                except Exception as exc:
                    resolved_symbols.append(
                        format_symbol_mention_failure(
                            symbol_name,
                            error=str(exc) or type(exc).__name__,
                        )
                    )

        context_blocks = []
        if resolved_files:
            context_blocks.append("Referenced files:\n" + "\n".join(resolved_files))
        if resolved_folders:
            context_blocks.append("Referenced folders:\n" + "\n".join(resolved_folders))
        if resolved_symbols:
            context_blocks.append("Referenced symbols:\n" + "\n".join(resolved_symbols))
        if resolved_codebases:
            context_blocks.append("Referenced codebase:\n" + "\n".join(resolved_codebases))

        if context_blocks:
            message = "\n\n".join(context_blocks) + "\n\n" + message

    pre = svc.pilot_preflight() if session_id is None else None
    if pre:
        handler.wfile.write(f"data: {json.dumps({'kind':'error','data':{'error':pre}})}\n\n".encode())
        handler.wfile.write(b"data: {\"kind\": \"done\"}\n\n")
        handler.wfile.flush()
        return

    from ..hooks import run_hooks
    # Bind turn identity before any view switch can reassign globals.
    ctx = {"session_id": turn_sid, "message": message, "pilot": turn_pilot, "config": turn_config, "repo": turn_config.repo}
    run_hooks("preRun", ctx)
    # Detach != cancel: if the client closes the EventSource mid-turn we keep
    # draining send() so its finally releases _busy. Closing the generator
    # early (old behavior) aborted the turn via GeneratorExit; cancel() on
    # BrokenPipe (auto path) stopped the governor for a mere view switch.
    # Explicit Stop still uses /api/session/interrupt.
    gen = turn_pilot.send(message, images=images or None, plan=plan, resume=resume, **receipt_args)
    last_ckpt = time.monotonic()

    def _maybe_checkpoint(ev):
        nonlocal last_ckpt
        # Incremental checkpoint: flush the transcript immediately when an
        # action result (incl. durable command-job pending receipt) was just
        # appended to history, else on a 2s throttle so a mid-turn crash
        # can't lose the last chunk of transcript.
        if should_force_transcript_checkpoint(ev):
            svc.checkpoint_transcript(ctx)
            last_ckpt = time.monotonic()
        elif time.monotonic() - last_ckpt >= 2.0:
            svc.checkpoint_transcript(ctx)
            last_ckpt = time.monotonic()

    try:
        ring = _sse_ring_begin(turn_sid)
        detached = sse_pump(
            handler.wfile,
            gen,
            _encode_chat_sse_frame,
            on_event=_maybe_checkpoint,
            write_done=False,
            ring=ring,
        )
        # Drain swarm_result / pilot_resume onto the live client before
        # framing done. Framing done is not itself detach.
        for ev in turn_pilot.drain_swarm_results():
            _maybe_checkpoint(ev)
            if not detached:
                if not sse_write(handler.wfile, _encode_chat_sse_frame(ev)):
                    detached = True
            try:
                ring.append(ev.kind, ev.data or {}, getattr(ev, "turn", None))
            except Exception as exc:
                _diag_note("stream_chat.ring_append", exc)
        if not detached:
            sse_write(handler.wfile, b"data: {\"kind\": \"done\"}\n\n")
        try:
            ring.append("done", {})
        except Exception as exc:
            _diag_note("stream_chat.ring_append_done", exc)
    finally:
        svc.finalize_turn(ctx)
