"""The plugin version, declared once.

``fastapi_audit.model.TOOL_VERSION`` and the design manifests read it from here; ``scripts/validate_structure.py``
checks it against the plugin manifests. It stays at the published version until a release increment bumps it.
"""

TOOL_VERSION = "2.1.0"
