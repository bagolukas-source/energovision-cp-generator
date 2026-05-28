"""Orkestra-style premium posudok (HTML→PDF cez WeasyPrint).

Replikuje 7-stranový A4 landscape PDF layout zo softvéru Orkestra
(app.orkestra.energy) — KPI hero + cashflow + energy flow diagram +
daily load + donut + monthly earnings + upfront costs.

Brand farby:
- Solar/FVE: žltá #FFD645
- Battery/BESS: zelená Energovision #16A34A
- Grid: modrá #5B7CFA
- Site/Consumption: fialová #B85DD8
- Pozadie sekcií: #F5F6F8
- Text primary: #1F2937
"""
from .generator import generate_orkestra_pdf, render_orkestra_html

__all__ = ["generate_orkestra_pdf", "render_orkestra_html"]
