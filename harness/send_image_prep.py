from __future__ import annotations

"""Native / sidecar image prep peeled from SendLoopMixin._send_locked_inner.

When the pilot can see pixels, keep OpenAI-shaped multimodal content
(text + image_url). Otherwise transcribe via sidecar. Either path must
fail loudly if images were attached but none become usable content —
never answer as silent text-only.

Generator return value is ``(processed_message, native_image_paths)`` on
success, or ``None`` after yielding an error event (caller should abort
the turn).
"""

from typing import Any, Iterator, Optional


def prepare_turn_images(
    session: Any,
    user_message: str,
    images: Optional[list],
) -> Iterator[Any]:
    """Yield vision ConvEvents; return ``(message, native_paths)`` or ``None``."""
    from .conversation import ConvEvent

    processed_message = user_message
    native_image_paths: list = []
    if not images:
        return processed_message, native_image_paths

    from .vision import (
        pilot_supports_native_images,
        resolve_provider_for_spec,
        transcribe_images,
    )

    provider = resolve_provider_for_spec(
        getattr(session.config, "driver", "") or ""
    )
    pilot_model = str(getattr(session.pilot, "model", "") or "")
    if pilot_supports_native_images(
        provider, model=pilot_model, pilot=session.pilot,
    ):
        yield ConvEvent("vision", {
            "count": len(images), "status": "native",
        })
        native_image_paths = [p for p in images if p]
        if not native_image_paths:
            err = (f"All {len(images)} image attachment(s) failed; "
                   "cannot answer an image request without pixels.")
            yield ConvEvent("error", {"error": err})
            return None
        for path in native_image_paths:
            yield ConvEvent("vision", {
                "path": path, "status": "native",
            })
    else:
        yield ConvEvent("vision", {
            "count": len(images), "status": "transcribing",
        })
        results = transcribe_images(images)
        blocks = []
        for path, r in zip(images, results):
            if r.error:
                yield ConvEvent("vision", {"path": path, "error": r.error})
            else:
                blocks.append(f"[Image: {path}]\n{r.text}")
                yield ConvEvent("vision", {"path": path,
                    "chars": len(r.text), "model": r.model,
                    "preview": r.text[:200]})
        if blocks:
            processed_message = (
                "The user attached image(s). Transcription(s) below "
                "(you cannot see the image, only this text):\n\n"
                + "\n\n".join(blocks) + "\n\n---\n" + user_message
            )
        else:
            # Every transcription failed. With no blocks the pilot
            # would silently answer as if no images were attached.
            err = (
                f"All {len(images)} image transcription(s) failed; "
                "cannot answer an image request as text-only."
            )
            yield ConvEvent("error", {"error": err})
            return None

    return processed_message, native_image_paths
