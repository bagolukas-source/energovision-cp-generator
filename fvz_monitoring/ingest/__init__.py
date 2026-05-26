"""FVZ Monitoring AI — ingestion package.

Multi-vendor data ingestion z cloud API:
- Huawei FusionSolar / SmartPVMS
- Solinteg iSolar
- GoodWe SEMS
- Fronius Solar.web
- Sungrow iSolarCloud

Všetky adaptéry implementujú VendorAdapter base class z base.py.
"""

from .base import VendorAdapter, VendorError, RateLimitError, AuthError
from .canonical import PlantInfo, TelemetrySnapshot, DailySummary, VendorAlarm

__all__ = [
    "VendorAdapter",
    "VendorError",
    "RateLimitError",
    "AuthError",
    "PlantInfo",
    "TelemetrySnapshot",
    "DailySummary",
    "VendorAlarm",
]
