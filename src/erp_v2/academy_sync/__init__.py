"""Academy -> ERP app-sync pilot: learning completion observations.

Destination-owned receiver for the `learning.completion.observation.v1`
capability (dotmac-app-sync). Completions only: progress updates stay on
Academy's existing v2 webhook until they gain durable event identity.
"""

from __future__ import annotations
