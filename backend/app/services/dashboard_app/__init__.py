"""Full-stack dashboard app pipeline services.

- generator: fills Jinja2 templates with a DashboardSpec → deployable app module
- manager: mounts/unmounts generated FastAPI sub-routers + lifecycle + pollers
- realtime: WebSocket connection manager + hash-based change detection
"""
