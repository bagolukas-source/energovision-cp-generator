"""Reporting engine v0.3 — premium HTML dashboard."""
from energovision_analytics.reporting.charts import (
    THEME,
    EnergovisionTheme,
    chart_battery_degradation,
    chart_bess_activity_breakdown,
    chart_carbon_summary,
    chart_cashflow,
    chart_energy_metric_area,
    chart_interval_activity,
    chart_interval_soc,
    chart_interval_spot,
    chart_monthly_earnings,
    chart_monthly_pv,
    chart_pv_consumption_donut,
    chart_site_consumption_donut,
    chart_soc_heatmap,
    chart_spaghetti_load,
    chart_tornado_sensitivity,
    chart_weekly_earnings,
    render_donut_legend,
    render_site_consumption_legend,
    render_week_detail_panel,
)
from energovision_analytics.reporting.html_dashboard import HTMLDashboard

__all__ = [
    "HTMLDashboard", "EnergovisionTheme", "THEME",
    "chart_pv_consumption_donut", "chart_site_consumption_donut",
    "chart_spaghetti_load", "chart_cashflow", "chart_monthly_earnings",
    "chart_weekly_earnings", "chart_carbon_summary", "chart_soc_heatmap",
    "chart_energy_metric_area", "chart_tornado_sensitivity",
    "chart_battery_degradation", "chart_bess_activity_breakdown",
    "chart_interval_activity", "chart_interval_soc", "chart_interval_spot",
    "chart_monthly_pv",
    "render_donut_legend", "render_site_consumption_legend",
    "render_week_detail_panel",
]
