"""Platform connector adapters.

Each adapter (`signal_adapter`, `telegram_adapter`, `whatsapp_adapter`) talks to
a sidecar connector container and normalizes its native payloads into the
`CanonicalEvent` shape defined in `connectors.base`. The app's `ingest.py` is the
single writer that turns `CanonicalEvent`s into rows in `messages` & friends.
"""
