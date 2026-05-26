/**
 * /dispatch/fleet — celkový prehľad všetkých 400 staníc.
 *
 * Vlastnosti:
 * - Mapa SK s pinmi (farba = health_status)
 * - Tabuľka staníc s filtrovaním (vendor, region, status, performance)
 * - Live update cez Supabase Realtime (subscribe na health_status zmeny)
 * - Klik na stanicu → /dispatch/site/[id]
 *
 * Použitie: page-level komponent v Next.js app routeri (app/dispatch/fleet/page.tsx).
 * Predpoklady: shadcn/ui, Tailwind, brand zelená (#16A34A), react-map-gl,
 * @supabase/supabase-js.
 */

"use client";

import { useEffect, useMemo, useState } from "react";
import { createClient, SupabaseClient } from "@supabase/supabase-js";

// === Typy ===================================================================

type FleetSite = {
  id: string;
  site_name: string;
  vendor: "huawei" | "solinteg" | "goodwe" | "fronius" | "sungrow";
  kw_dc_nominal: number;
  battery_kwh_nominal: number | null;
  lat: number | null;
  lon: number | null;
  distribution_area: string | null;
  zero_export_required: boolean;
  last_seen_at: string | null;
  health_status: "ok" | "warn" | "alarm" | "offline" | "unknown";
  customer_name: string | null;
  energy_kwh_yesterday: number | null;
  pr_yesterday: number | null;
  open_alarms_count: number;
};

const HEALTH_COLORS: Record<FleetSite["health_status"], string> = {
  ok: "#16A34A",       // brand zelená
  warn: "#F59E0B",
  alarm: "#DC2626",
  offline: "#6B7280",
  unknown: "#9CA3AF",
};

// === Supabase klient =========================================================

const supabase: SupabaseClient = createClient(
  process.env.NEXT_PUBLIC_SUPABASE_URL!,
  process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,
);

// === Komponent ===============================================================

export default function FleetPage() {
  const [sites, setSites] = useState<FleetSite[]>([]);
  const [loading, setLoading] = useState(true);
  const [filterStatus, setFilterStatus] = useState<string>("all");
  const [filterVendor, setFilterVendor] = useState<string>("all");
  const [searchTerm, setSearchTerm] = useState<string>("");

  // Načítaj stanice
  useEffect(() => {
    let mounted = true;
    (async () => {
      const { data, error } = await supabase
        .from("v_fleet_status")
        .select("*")
        .order("health_status", { ascending: true })
        .limit(1000);
      if (mounted && !error) {
        setSites((data || []) as FleetSite[]);
        setLoading(false);
      }
    })();

    // Realtime subscription — bude push pri zmene health_status
    const channel = supabase
      .channel("fleet-status")
      .on(
        "postgres_changes",
        { event: "UPDATE", schema: "public", table: "inverter_sites" },
        (payload) => {
          setSites((current) =>
            current.map((s) =>
              s.id === payload.new.id ? { ...s, health_status: payload.new.health_status } : s,
            ),
          );
        },
      )
      .subscribe();

    return () => {
      mounted = false;
      supabase.removeChannel(channel);
    };
  }, []);

  // Filtrovanie
  const filtered = useMemo(() => {
    return sites.filter((s) => {
      if (filterStatus !== "all" && s.health_status !== filterStatus) return false;
      if (filterVendor !== "all" && s.vendor !== filterVendor) return false;
      if (
        searchTerm &&
        !s.site_name.toLowerCase().includes(searchTerm.toLowerCase()) &&
        !(s.customer_name || "").toLowerCase().includes(searchTerm.toLowerCase())
      )
        return false;
      return true;
    });
  }, [sites, filterStatus, filterVendor, searchTerm]);

  // Štatistika
  const stats = useMemo(() => {
    const total = sites.length;
    const ok = sites.filter((s) => s.health_status === "ok").length;
    const warn = sites.filter((s) => s.health_status === "warn").length;
    const alarm = sites.filter((s) => s.health_status === "alarm").length;
    const offline = sites.filter((s) => s.health_status === "offline").length;
    const totalCapacity = sites.reduce((sum, s) => sum + (s.kw_dc_nominal || 0), 0);
    const yesterdayEnergy = sites.reduce((sum, s) => sum + (s.energy_kwh_yesterday || 0), 0);
    return { total, ok, warn, alarm, offline, totalCapacity, yesterdayEnergy };
  }, [sites]);

  return (
    <div className="min-h-screen bg-gray-50 p-6">
      {/* Header */}
      <div className="mb-6">
        <h1 className="text-2xl font-semibold text-gray-900">Dispečing — Fleet view</h1>
        <p className="text-sm text-gray-500 mt-1">
          {stats.total} staníc · {stats.totalCapacity.toFixed(1)} kWp inštalovaných ·{" "}
          {stats.yesterdayEnergy.toFixed(0)} kWh vyrobených včera
        </p>
      </div>

      {/* Health summary cards */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-3 mb-6">
        <StatCard label="V poriadku" value={stats.ok} color="#16A34A" />
        <StatCard label="Upozornenie" value={stats.warn} color="#F59E0B" />
        <StatCard label="Alarm" value={stats.alarm} color="#DC2626" />
        <StatCard label="Offline" value={stats.offline} color="#6B7280" />
        <StatCard label="Celkom" value={stats.total} color="#1F2937" />
      </div>

      {/* Filtre */}
      <div className="flex flex-wrap gap-3 mb-4">
        <input
          type="text"
          placeholder="Hľadaj stanicu alebo zákazníka..."
          className="px-3 py-2 border rounded-lg text-sm flex-grow min-w-[200px]"
          value={searchTerm}
          onChange={(e) => setSearchTerm(e.target.value)}
        />
        <select
          className="px-3 py-2 border rounded-lg text-sm"
          value={filterStatus}
          onChange={(e) => setFilterStatus(e.target.value)}
        >
          <option value="all">Všetky statusy</option>
          <option value="ok">V poriadku</option>
          <option value="warn">Upozornenie</option>
          <option value="alarm">Alarm</option>
          <option value="offline">Offline</option>
        </select>
        <select
          className="px-3 py-2 border rounded-lg text-sm"
          value={filterVendor}
          onChange={(e) => setFilterVendor(e.target.value)}
        >
          <option value="all">Všetci vendori</option>
          <option value="huawei">Huawei</option>
          <option value="solinteg">Solinteg</option>
          <option value="goodwe">GoodWe</option>
          <option value="fronius">Fronius</option>
          <option value="sungrow">Sungrow</option>
        </select>
      </div>

      {/* Mapa (placeholder — pridať react-map-gl alebo Leaflet) */}
      <div className="bg-white rounded-lg shadow-sm p-4 mb-4 h-64 flex items-center justify-center text-gray-400 text-sm">
        [Tu pôjde mapa SK s pinmi pre {filtered.length} staníc — pridať react-map-gl / Maptiler /
        Mapbox v ďalšom kroku]
      </div>

      {/* Tabuľka */}
      <div className="bg-white rounded-lg shadow-sm overflow-hidden">
        {loading ? (
          <div className="p-8 text-center text-gray-400">Načítavam…</div>
        ) : (
          <table className="w-full text-sm">
            <thead className="bg-gray-50 text-left text-xs uppercase text-gray-500">
              <tr>
                <th className="px-4 py-2">Status</th>
                <th className="px-4 py-2">Stanica</th>
                <th className="px-4 py-2">Zákazník</th>
                <th className="px-4 py-2">Vendor</th>
                <th className="px-4 py-2 text-right">kWp</th>
                <th className="px-4 py-2 text-right">Včera kWh</th>
                <th className="px-4 py-2 text-right">PR</th>
                <th className="px-4 py-2 text-right">Alarmy</th>
                <th className="px-4 py-2">Posledne videné</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((s) => (
                <tr
                  key={s.id}
                  className="border-t hover:bg-green-50 cursor-pointer"
                  onClick={() => (window.location.href = `/dispatch/site/${s.id}`)}
                >
                  <td className="px-4 py-2">
                    <span
                      className="inline-block w-3 h-3 rounded-full"
                      style={{ backgroundColor: HEALTH_COLORS[s.health_status] }}
                    />
                  </td>
                  <td className="px-4 py-2 font-medium text-gray-900">{s.site_name}</td>
                  <td className="px-4 py-2 text-gray-600">{s.customer_name || "—"}</td>
                  <td className="px-4 py-2 capitalize">{s.vendor}</td>
                  <td className="px-4 py-2 text-right">{s.kw_dc_nominal?.toFixed(1)}</td>
                  <td className="px-4 py-2 text-right">{s.energy_kwh_yesterday?.toFixed(0) || "—"}</td>
                  <td className="px-4 py-2 text-right">
                    {s.pr_yesterday ? (
                      <span
                        className={
                          s.pr_yesterday < 0.7
                            ? "text-red-600"
                            : s.pr_yesterday < 0.8
                              ? "text-yellow-600"
                              : "text-green-600"
                        }
                      >
                        {s.pr_yesterday.toFixed(2)}
                      </span>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td className="px-4 py-2 text-right">
                    {s.open_alarms_count > 0 ? (
                      <span className="px-2 py-0.5 bg-red-100 text-red-700 rounded text-xs">
                        {s.open_alarms_count}
                      </span>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td className="px-4 py-2 text-xs text-gray-500">
                    {s.last_seen_at ? new Date(s.last_seen_at).toLocaleString("sk") : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

// === Helper komponent =========================================================

function StatCard({ label, value, color }: { label: string; value: number; color: string }) {
  return (
    <div className="bg-white rounded-lg shadow-sm p-3 border-l-4" style={{ borderColor: color }}>
      <div className="text-xs text-gray-500">{label}</div>
      <div className="text-2xl font-semibold mt-1" style={{ color }}>
        {value}
      </div>
    </div>
  );
}
