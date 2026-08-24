# Marionette browser relay (unpacked)

Opt-in Chrome extension + native-host message for the existing harness browser
stack (`harness.browser` / CDP). Not a Chrome Web Store listing.

Enable the inbound recorder with `PM_BROWSER_RELAY=1`. The harness endpoint is
`POST /api/browser/relay` (auth token required, same as other API routes).

## Message shape

```json
{
  "kind": "tab_snapshot",
  "url": "https://example.com/",
  "title": "Example",
  "text": "optional page text",
  "tab_id": 12,
  "source": "extension"
}
```

`source` is `extension` or `native_host`. `text` is optional.

Load this folder as an unpacked extension. The native host (`native-host.py`)
speaks Chrome's length-prefixed JSON protocol and forwards the same object.
