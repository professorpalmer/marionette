# Testing the Puppetmaster consumer integration

The consumer changes use the bounded metadata contracts in Puppetmaster development commit `057a786b0e53302ceff0a271b8af73d86864c664`. Release dependency pins are separate from this development setup. Publish a 1.25.0 pin only after that version is available on PyPI.

Use an isolated environment in the Marionette checkout:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pip install --no-deps -e ../Puppetmaster
```

Select the intended Puppetmaster development revision before the second install. To check which source Python actually imports:

```sh
env -u PYTHONPATH .venv/bin/python -c 'import puppetmaster, importlib.metadata; print(puppetmaster.__file__); print(importlib.metadata.version("puppetmaster-ai"))'
```

Clear inherited `PYTHONPATH` for these checks. A worker host can otherwise prepend its installed Puppetmaster and hide the package in the isolated environment.

The desktop bootstrap already accepts `MARIONETTE_PUPPETMASTER_SPEC`. For development bootstrap, set it to an absolute local Puppetmaster checkout path. This changes the local installation receipt without editing the release pin.

The supported inspector shows bounded task and artifact references, captured attempt/run/process history, publication receipts, and selected terminal economics. Captured models are historical; selected receipt totals do not represent all attempts or live session spend. The current bounded API does not expose current PM routing assignments, routing policies and rejected alternatives, artifact verification diagnoses, PM job timestamps, or live aggregate economics.

The transport, service, native observation/control, and shared UI changes must be reviewed together for deployment. The service can report unsupported-runtime unavailability on older installations; enabling the new UI with full metadata requires the compatible local development runtime or the later published dependency update.
