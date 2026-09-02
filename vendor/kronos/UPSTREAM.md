# Vendored Kronos inference code

This directory contains the upstream `model/` inference package and MIT
license from `shiyu-coder/Kronos` at commit
`67b630e67f6a18c9e9be918d9b4337c960db1e9a`.

Only the small inference source is vendored. Model and tokenizer weights are
never committed to this repository; the optional Windows installer downloads
the pinned Hugging Face revisions into a user-local cache.

Local modification: the unused top-level Pandas import was removed. The
technical-system sidecar calls the upstream tensor inference function directly
with Polars/NumPy arrays and does not use the Pandas convenience predictor.

- Source: https://github.com/shiyu-coder/Kronos
- Model: `NeoQuasar/Kronos-base` at `2b554741eca47781b64468546e77fef3e85130e6`
- Tokenizer: `NeoQuasar/Kronos-Tokenizer-base` at `0e0117387f39004a9016484a186a908917e22426`
- License: MIT; see `LICENSE` in this directory.
