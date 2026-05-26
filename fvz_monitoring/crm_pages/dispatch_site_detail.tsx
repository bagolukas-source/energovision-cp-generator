/**
 * /dispatch/site/[id] — detail jednej FVE stanice.
 *
 * Layout:
 *  - Header: meno stanice, status badge, vendor, kWp, batéria, klient
 *  - 4 KPI cards: dnes vyrobené, PR, peak power, batéria SoC
 *  - Live graf výroby za posledných 24h (ApexCharts / Recharts)
 *  - Mesačná tabuľka výnosov + PR
 *  - Open alarms list
 *  - Posledná telemetria (raw, debug)
 *
 * Použitie: Next.js dynamic route app/dispatch/site/[id]/page.tsx
 */

"use client";

import { useEffect, useState } from "react";
import { createClient } from "@supabase/supabase-js";
import {
  LineChart, Line, XAxis, YAxis, Tooltip, CartesianGrid, ResponsiveContainer, Legend,
} from "recharts";

const supabase = createClient(
  process.env.NEXT_PUBLIC_SUPABASE_URL!,
  process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,
);

export default function SiteDetailPage({ params }: { params: { id: string } }) {
  const siteId = params.id;
  const [site, setSite] = useState<any>(null);
  const [telemetry, setTelemetry] = useState<any[]>([]);
  const [kpiToday, setKpiToday] = useState<any>(null);
  const [alarms, setAlarms] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    (async () => {
      // Master data
      const { data: s } = await supabase
        .from("v_fleet_status")
        .select("*")
        .eq("id", siteId)
        .single();
      setSite(s);

      // Telemetria — posledných 24h (15min agregát)
      const from = new Date(Date.now() - 24 * 3600 * 1000).toISOString();
      const { data: t } = await supabase
        .from("telemetry_15min")
        .select("ts, ac_power_kw_avg, battery_soc_avg, grid_export_kwh, grid_import_kwh")
        .eq("site_id", siteId)
        .gte("ts", from)
        .order("ts");
      setTelemetry(t || []);

      // KPI včera
      const yesterday = new Date(Date.now() - 24 * 3600 * 1000).toISOString().slice(0, 10);
      const { data: k } = await supabase
        .from("performance_kpis_daily")
        .select("*")
        .eq("site_id", siteId)
        .eq("day", yesterday)
        .maybeSingle();
      setKpiToday(k);

      // Open alarms
      const { data: a } = await supabase
        .from("alarms")
        .select("*")
        .eq("site_id", siteId)
        .is("resolved_at", null)
        .order("detected_at", { ascending: false });
      setAlarms(a || []);

      setLoading(false);
    })();
  }, [siteId]);

  // Realtime — push pri nových telemetry záznamoch
  useEffect(() => {
    const channel = supabase
      .channel(`site-${siteId}`)
      .on(
        "postgres_changes",
        { event: "INSERT", schema: "public", table: "telemetry_5min", filter: `site_id=eq.${siteId}` },
        (payload) => {
          // Tu by sme mohli prepnúť na fresh data alebo append
        },
      )
      .subscribe();
    return () => {
      supabase.removeChannel(channel);
    };
  }, [siteId]);

  if (loading) return <div className="p-8 text-gray-400">Načítavam…</div>;
  if (!site) return <div className="p-8 text-gray-400">Stanica nenájdená</div>;

  const statusColor =
    site.health_status === "ok"
      ? "bg-green-100 text-green-700"
      : site.health_status === "warn"
        ? "bg-yellow-100 text-yellow-700"
        : site.health_status === "alarm"
          ? "bg-red-100 text-red-700"
          : "bg-gray-100 text-gray-700";

  return (
    <div className="min-h-screen bg-gray-50 p-6 space-y-6">
      {/* Header */}
      <div className="flex justify-between items-start">
        <div>
          <h1 className="text-2xl font-semibold">{site.site_name}</h1>
          <p className="text-sm text-gray-500 mt-1">
            {site.customer_name} · {site.vendor} · {site.dc_kwp} kWp
            {site.bess_kwh ? ` · ${site.bess_kwh} kWh batéria` : ""}
          </p>
        </div>
        <span className={`px-3 py-1 rounded-full text-sm font-medium ${statusColor}`}>
          {site.health_status.toUpperCase()}
        </span>
      </div>

      {/* KPI cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <Kpi label="Energia včera" value={kpiToday?.energy_kwh?.toFixed(1) || "—"} unit="kWh" />
        <Kpi
          label="Performance Ratio"
          value={kpiToday?.performance_ratio?.toFixed(2) || "—"}
          subtitle={
            kpiToday?.cohort_pr_median
              ? `Kohorta: ${kpiToday.cohort_pr_median.toFixed(2)}`
              : undefined
          }
        />
        <Kpi label="Peak power" value={kpiToday?.peak_power_kw?.toFixed(1) || "—"} unit="kW" />
        <Kpi
          label="Samospotreba"
          value={kpiToday?.self_consumption_pct?.toFixed(0) || "—"}
          unit="%"
        />
      </div>

      {/* Graf 24h */}
      <div className="bg-white p-4 rounded-lg shadow-sm">
        <h2 className="text-sm font-semibold mb-3">Výroba za posledných 24 hodín</h2>
        <ResponsiveContainer width="100%" height={280}>
          <LineChart data={telemetry}>
            <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" />
            <XAxis
              dataKey="ts"
              tickFormatter={(t) => new Date(t).toLocaleTimeString("sk", { hour: "2-digit", minute: "2-digit" })}
              tick={{ fontSize: 11 }}
            />
            <YAxis tick={{ fontSize: 11 }} />
            <Tooltip
              labelFormatter={(t) => new Date(t).toLocaleString("sk")}
              formatter={(value: any, name: string) => [
                typeof value === "number" ? value.toFixed(2) : value,
                name,
              ]}
            />
            <Legend />
            <Line
              type="monotone"
              dataKey="ac_power_kw_avg"
              name="AC výkon (kW)"
              stroke="#16A34A"
              strokeWidth={2}
              dot={false}
            />
            <Line
              type="monotone"
              dataKey="battery_soc_avg"
              name="Batéria SoC (%)"
              stroke="#F59E0B"
              strokeWidth={2}
              dot={false}
              yAxisId={0}
            />
          </LineChart>
        </ResponsiveContainer>
      </div>

      {/* Open alarms */}
      <div className="bg-white p-4 rounded-lg shadow-sm">
        <h2 className="text-sm font-semibold mb-3">Otvorené alarmy ({alarms.length})</h2>
        {alarms.length === 0 ? (
          <p className="text-sm text-gray-400">Žiadne otvorené alarmy.</p>
        ) : (
          <ul className="divide-y">
            {alarms.map((a) => (
              <li key={a.id} className="py-2 flex justify-between items-start">
                <div>
                  <p className="font-medium">{a.title}</p>
                  <p className="text-xs text-gray-500 mt-0.5">
                    {new Date(a.detected_at).toLocaleString("sk")} · {a.category}
                  </p>
                  {a.description && (
                    <p className="text-sm text-gray-700 mt-1">{a.description}</p>
                  )}
                </div>
                <span
                  className={`text-xs px-2 py-0.5 rounded ${
                    a.severity === "critical"
                      ? "bg-red-100 text-red-700"
                      : a.severity === "major"
                        ? "bg-orange-100 text-orange-700"
                        : "bg-yellow-100 text-yellow-700"
                  }`}
                >
                  {a.severity}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function Kpi({
  label,
  value,
  unit,
  subtitle,
}: {
  label: string;
  value: string | number;
  unit?: string;
  subtitle?: string;
}) {
  return (
    <div className="bg-white p-4 rounded-lg shadow-sm">
      <div className="text-xs text-gray-500">{label}</div>
      <div className="text-2xl font-semibold mt-1">
        {value}
        {unit && <span className="text-sm text-gray-500 ml-1">{unit}</span>}
      </div>
      {subtitle && <div className="text-xs text-gray-400 mt-1">{subtitle}</div>}
    </div>
  );
}
