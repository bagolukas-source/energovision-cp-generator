/**
 * /dispatch/alarms — alarm inbox + routing + ack.
 *
 * Funkcie:
 *  - Zoznam open alarmov so severity stĺpcom
 *  - Filter: severity, vendor, category, assigned-to-me
 *  - Akcie: Acknowledge, Resolve, Assign to me, Add note
 *  - Realtime push pri novom alarme (Supabase Realtime + browser notification)
 *  - Detail panel: linka na stanicu, root cause confidence, auto_actions_taken
 */

"use client";

import { useEffect, useMemo, useState } from "react";
import { createClient } from "@supabase/supabase-js";

const supabase = createClient(
  process.env.NEXT_PUBLIC_SUPABASE_URL!,
  process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,
);

type Alarm = {
  id: string;
  site_id: string;
  site_name?: string;
  vendor: string;
  severity: "info" | "warn" | "minor" | "major" | "critical";
  category: string;
  title: string;
  description: string | null;
  detected_at: string;
  resolved_at: string | null;
  acknowledged_at: string | null;
  assigned_to: string | null;
  root_cause: string | null;
  root_cause_confidence: number | null;
};

const SEVERITY_RANK: Record<Alarm["severity"], number> = {
  critical: 0, major: 1, minor: 2, warn: 3, info: 4,
};

export default function AlarmsPage() {
  const [alarms, setAlarms] = useState<Alarm[]>([]);
  const [filterSeverity, setFilterSeverity] = useState("all");
  const [filterCategory, setFilterCategory] = useState("all");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    (async () => {
      const { data } = await supabase
        .from("alarms")
        .select("*, inverter_sites(site_name)")
        .is("resolved_at", null)
        .is("deduplicated_into", null)
        .order("detected_at", { ascending: false })
        .limit(500);
      const flat = (data || []).map((a: any) => ({
        ...a,
        site_name: a.inverter_sites?.site_name,
      }));
      flat.sort(
        (a: Alarm, b: Alarm) =>
          SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity] ||
          new Date(b.detected_at).getTime() - new Date(a.detected_at).getTime(),
      );
      setAlarms(flat);
      setLoading(false);
    })();

    // Realtime — nový alarm okamžite na obrazovku
    const channel = supabase
      .channel("alarms-stream")
      .on(
        "postgres_changes",
        { event: "INSERT", schema: "public", table: "alarms" },
        async (payload) => {
          // Browser push notification
          if (typeof Notification !== "undefined" && Notification.permission === "granted") {
            new Notification("Nový alarm", { body: payload.new.title });
          }
          // Refresh
          const { data } = await supabase
            .from("alarms")
            .select("*, inverter_sites(site_name)")
            .eq("id", payload.new.id)
            .single();
          if (data) {
            setAlarms((cur) => [{ ...data, site_name: (data as any).inverter_sites?.site_name }, ...cur]);
          }
        },
      )
      .subscribe();

    // Request notification permission
    if (typeof Notification !== "undefined" && Notification.permission === "default") {
      Notification.requestPermission();
    }

    return () => {
      supabase.removeChannel(channel);
    };
  }, []);

  const filtered = useMemo(() => {
    return alarms.filter((a) => {
      if (filterSeverity !== "all" && a.severity !== filterSeverity) return false;
      if (filterCategory !== "all" && a.category !== filterCategory) return false;
      return true;
    });
  }, [alarms, filterSeverity, filterCategory]);

  async function acknowledge(id: string) {
    await supabase
      .from("alarms")
      .update({ acknowledged_at: new Date().toISOString() })
      .eq("id", id);
    setAlarms((cur) =>
      cur.map((a) => (a.id === id ? { ...a, acknowledged_at: new Date().toISOString() } : a)),
    );
  }

  async function resolve(id: string) {
    await supabase
      .from("alarms")
      .update({ resolved_at: new Date().toISOString() })
      .eq("id", id);
    setAlarms((cur) => cur.filter((a) => a.id !== id));
  }

  const categories = Array.from(new Set(alarms.map((a) => a.category))).sort();

  return (
    <div className="min-h-screen bg-gray-50 p-6">
      <div className="mb-6">
        <h1 className="text-2xl font-semibold">Dispečing — Alarmy</h1>
        <p className="text-sm text-gray-500 mt-1">{filtered.length} otvorených alarmov</p>
      </div>

      {/* Filtre */}
      <div className="flex gap-3 mb-4">
        <select
          value={filterSeverity}
          onChange={(e) => setFilterSeverity(e.target.value)}
          className="px-3 py-2 border rounded-lg text-sm"
        >
          <option value="all">Všetky severity</option>
          <option value="critical">Critical</option>
          <option value="major">Major</option>
          <option value="minor">Minor</option>
          <option value="warn">Warn</option>
        </select>
        <select
          value={filterCategory}
          onChange={(e) => setFilterCategory(e.target.value)}
          className="px-3 py-2 border rounded-lg text-sm"
        >
          <option value="all">Všetky kategórie</option>
          {categories.map((c) => (
            <option key={c} value={c}>{c}</option>
          ))}
        </select>
      </div>

      {/* Zoznam */}
      <div className="space-y-2">
        {loading && <p className="text-gray-400">Načítavam…</p>}
        {filtered.map((a) => (
          <div
            key={a.id}
            className="bg-white rounded-lg shadow-sm p-4 flex justify-between items-start"
          >
            <div className="flex-grow">
              <div className="flex items-center gap-2">
                <SeverityBadge sev={a.severity} />
                <a
                  href={`/dispatch/site/${a.site_id}`}
                  className="font-medium text-gray-900 hover:text-green-700"
                >
                  {a.site_name || a.site_id}
                </a>
                <span className="text-xs text-gray-500">· {a.category}</span>
              </div>
              <p className="mt-1 text-gray-800">{a.title}</p>
              {a.description && <p className="mt-1 text-sm text-gray-600">{a.description}</p>}
              {a.root_cause_confidence && (
                <p className="mt-1 text-xs text-gray-500">
                  AI klasifikácia: {a.root_cause} ({(a.root_cause_confidence * 100).toFixed(0)}% confidence)
                </p>
              )}
              <p className="mt-1 text-xs text-gray-400">
                {new Date(a.detected_at).toLocaleString("sk")}
              </p>
            </div>
            <div className="flex gap-2 ml-4">
              {!a.acknowledged_at && (
                <button
                  onClick={() => acknowledge(a.id)}
                  className="px-3 py-1 text-sm border rounded hover:bg-gray-50"
                >
                  Ack
                </button>
              )}
              <button
                onClick={() => resolve(a.id)}
                className="px-3 py-1 text-sm bg-green-600 text-white rounded hover:bg-green-700"
              >
                Resolve
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function SeverityBadge({ sev }: { sev: Alarm["severity"] }) {
  const style: Record<string, string> = {
    critical: "bg-red-100 text-red-700",
    major: "bg-orange-100 text-orange-700",
    minor: "bg-yellow-100 text-yellow-700",
    warn: "bg-blue-100 text-blue-700",
    info: "bg-gray-100 text-gray-700",
  };
  return (
    <span className={`text-xs px-2 py-0.5 rounded font-medium ${style[sev]}`}>
      {sev.toUpperCase()}
    </span>
  );
}
