"""Long-running process entrypoints (scheduler, reconciler) that sit alongside the
FastAPI app. Each module is runnable standalone via `python -m app.services.<name>`
and is also imported by tests / docker-compose services."""
