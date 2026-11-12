# Why Marionette

## There is no best model

Hot-swap the driver by task.
A cheap model handles inline work, at cents per session (for example
qwen3-coder-30b through OpenRouter).
Heavy reasoning and multi-file changes go to Puppetmaster workers that the
router picks.
The LLM is a component inside the kernel. It is not the platform.

## Aligned incentives: a kernel, not a model vendor

A first-party coding tool from a model lab sells you tokens. Every
optimization that cuts your token bill cuts their revenue. They build prompt
caching because the competition forces it, then stop where "competitive"
ends. Cached reads bill at about 10% of input, not free, and the savings
are rarely shown to you.

Marionette makes nothing from your token count. So its incentives point
one way: spend less, and prove it.

- **Send less.** CodeGraph-first retrieval injects targeted symbols, not
  whole files, so prompts start small.
- **Re-send cheap.** Multi-provider prompt caching (stable + moving
  breakpoints) serves the large, unchanging prefix from cache every turn.
- **Bill honestly.** Cached tokens are priced at the real cache-read
  discount, and the dollars saved show live in the status bar.
- **Route to the cheapest model that is good enough**, including a
  competitor's open-weights model. A model vendor never routes you off its
  own tokens. An independent kernel does.

The measured side of this is in [Benchmarks](../evidence/benchmarks.md).
The longer thesis, with the seam proof, is in
[Architecture](architecture.md#1-the-thesis).
