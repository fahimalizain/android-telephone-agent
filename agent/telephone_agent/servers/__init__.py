"""Protocol adapters for the long-lived control plane.

The package deliberately imports nothing at module load. The MCP and A2A
adapters depend on optional packages from the ``server`` extra, and importing
:mod:`telephone_agent` must keep working without that extra installed.
"""

__all__: list[str] = []
