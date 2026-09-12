/** User-facing recovery copy; never expose error bodies or backend paths. */
const UNBOUND_SESSION_MESSAGE =
  "Open a workspace or pick a project session before sending. Your draft is retained.";

function failureCode(error: unknown): string | null {
  if (!error || typeof error !== "object") return null;
  const body = "body" in error ? error.body : error;
  if (!body || typeof body !== "object" || !("code" in body) || typeof body.code !== "string") {
    return null;
  }
  return body.code;
}

export function inputFailureMessage(error: unknown): string | null {
  const code = failureCode(error);
  if (!code) return null;
  switch (code) {
    case "input_stopped":
      return "Stop cancelled this input. Review your draft before sending again.";
    case "input_session_changed":
      return "The active session changed. Return to the intended session and review your draft before sending.";
    case "input_stash_expired":
      return "Input staging expired. Review your draft and send it again.";
    case "input_attachment_limit":
      return "Attachment limits exceeded. Reduce the attachments in your draft before sending.";
    case "input_invalid":
    case "input_attachment_invalid":
      return "The input could not be accepted. Check your draft and attachments before sending.";
    case "input_attachment_corrupt":
      return "An attachment no longer matches its saved original. Remove or re-attach the file, then review your draft before sending.";
    case "input_attachment_unavailable":
    case "input_attachment_unknown":
      return "An attachment is missing or unavailable. Re-attach the file and review your draft before sending.";
    case "input_commit_uncertain":
    case "input_delivery_uncertain":
    case "input_stop_uncertain":
    case "input_publication_conflict":
      return "Input delivery could not be confirmed. Review your draft before sending again.";
    case "input_held":
    case "input_already_attempted":
    case "input_handoff_conflict":
    case "input_terminal":
      return "This input is held or has already been attempted. Review your draft before sending again.";
    case "input_retry_conflict":
    case "input_id_conflict":
      return "This input identity belongs to a different submission. Review your draft before sending again.";
    case "input_document_missing":
      return "Saved input originals are missing and could not be restored. Review your draft, then send again from an open project session.";
    case "input_lock_failed":
    case "input_storage_unavailable":
      return "Input storage is temporarily unavailable. Keep your draft and try again in a moment.";
    case "input_owner_required":
    case "input_owner_invalid":
    case "queue_session_unbound":
      return UNBOUND_SESSION_MESSAGE;
    case "pilot_not_ready":
      return "No project session is ready yet. Open a workspace or pick a project session, then review your draft before sending.";
    case "input_corrupt":
    case "input_read_failed":
      return "Input originals could not be verified. Evidence was left unchanged — review your draft before sending again.";
    case "input_archive_unavailable":
    case "input_archive_stale":
      return "Archived input originals could not be restored. Keep your draft and send again from the intended session.";
    case "input_evidence_missing":
    case "input_evidence_unreadable":
    case "input_transcript_unreadable":
      return "Input evidence could not be read. Keep your draft and review it before sending again.";
    case "input_restore_conflict":
    case "input_restore_required":
      return "Input restore could not complete safely. Keep your draft and review it before sending again.";
    case "input_transition_invalid":
    case "input_unknown":
      return "This turn's input delivery was interrupted. Continue or Retry to send a fresh attempt.";
    default:
      return code.startsWith("input_")
        ? "The input needs review. Review your draft before sending again."
        : null;
  }
}
