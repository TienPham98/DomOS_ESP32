"use client";

import {
  Monitor,
  Wifi,
  HardDrive,
  Cpu,
  Thermometer,
  Clock,
  Activity,
  ArrowUpRight,
} from "lucide-react";
import Link from "next/link";
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
} from "recharts";

import { PageHeader, MetricCard, Section, StatusBadge, StorageBar } from "@/components/dashboard-primitives";
import { useBoard } from "@/hooks/use-board";

export default function DashboardPage() {
  const { board, deviceList, logs, telemetry, error } = useBoard();


  const devices = deviceList;
  const totalCount = devices.length;
  const storagePercent = board?.storage_total
    ? Math.round(((board.storage_used ?? 0) / board.storage_total) * 100)
    : null;

  return (
    <>
      <PageHeader
        title="Dashboard"
        subtitle="ES3C28P Fleet Control"
        badge={board ? "Connected via Cloud" : error ? "Disconnected" : "Connecting..."}
      />

      {/* ── Metric Cards ───────────────────────────── */}
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4 mb-8">
        <MetricCard
          icon={Monitor}
          label="Devices"
          value={totalCount}
          detail={board ? "1 online via AI Gateway" : "0 online · 0 offline"}
          color="cyan"
          trend="up"
          delay={0}
        />
        <MetricCard
          icon={Wifi}
          label="Avg. RSSI"
          value="--"
          detail={board ? "Cloud WebSocket session" : "Signal strength unavailable"}
          color="green"
          trend="neutral"
          delay={60}
        />
        <MetricCard
          icon={Cpu}
          label="Firmware"
          value={board ? `v${board.firmware}` : "--"}
          detail={board ? `Board: ${board.board}` : "Latest OTA channel"}
          color="purple"
          trend="neutral"
          delay={120}
        />
        <MetricCard
          icon={HardDrive}
          label="Avg. Storage"
          value={storagePercent !== null ? `${storagePercent}%` : "--"}
          detail={
            typeof board?.free_heap === "number"
              ? `Heap Free: ${Math.round(board.free_heap / 1024)} KB`
              : board
                ? "Telemetry unavailable"
                : "LittleFS usage across fleet"
          }
          color="orange"
          delay={180}
        />
      </div>

      <div className="grid gap-6 xl:grid-cols-3 mb-8">
        {/* ── RSSI Chart ───────────────────────────── */}
        <Section title="Signal Strength (24h)">
          <div className="h-52">
            <div className="h-full flex items-center justify-center text-slate-500 text-sm font-medium">
              Signal telemetry is available only on the local network
            </div>
          </div>
        </Section>

        {/* ── Temperature Chart ────────────────────── */}
        <Section title="CPU Temperature (24h)">
          <div className="h-52">
            <div className="h-full flex items-center justify-center text-slate-500 text-sm font-medium">
              Temperature telemetry is not reported by this board
            </div>
          </div>
        </Section>

        {/* ── Free Heap Chart ──────────────────────── */}
        <Section title="Free Heap (24h)">
          <div className="h-52">
            {telemetry.length === 0 ? (
              <div className="h-full flex items-center justify-center text-slate-500 text-sm font-medium">
                No memory telemetry data
              </div>
            ) : (
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={telemetry}>
                  <defs>
                    <linearGradient id="heapGrad" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="#a855f7" stopOpacity={0.3} />
                      <stop offset="100%" stopColor="#a855f7" stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <XAxis
                    dataKey="time"
                    stroke="#334155"
                    tick={{ fill: "#64748b", fontSize: 11 }}
                    axisLine={false}
                    tickLine={false}
                  />
                  <YAxis
                    stroke="#334155"
                    tick={{ fill: "#64748b", fontSize: 11 }}
                    axisLine={false}
                    tickLine={false}
                    tickFormatter={(v: number) => `${(v / 1000).toFixed(0)}k`}
                  />
                  <Tooltip
                    contentStyle={{
                      background: "#0b1528",
                      border: "1px solid #1a2d4d",
                      borderRadius: 12,
                      color: "#e2e8f0",
                      fontSize: 13,
                    }}
                    formatter={(v) => [`${(Number(v) / 1000).toFixed(1)}k bytes`, "Free Heap"]}
                  />
                  <Area
                    type="monotone"
                    dataKey="heap"
                    stroke="#a855f7"
                    strokeWidth={2}
                    fill="url(#heapGrad)"
                    dot={false}
                    activeDot={{ r: 4, fill: "#a855f7" }}
                  />
                </AreaChart>
              </ResponsiveContainer>
            )}
          </div>
        </Section>
      </div>

      <div className="grid gap-6 xl:grid-cols-2 mb-8">
        {/* ── Device List ──────────────────────────── */}
        <Section
          title="Devices"
          action={
            <Link
              href="/devices"
              className="flex items-center gap-1 text-xs text-cyan-400 hover:text-cyan-300 transition"
            >
              View all <ArrowUpRight className="w-3 h-3" />
            </Link>
          }
        >
          <div className="space-y-3">
            {devices.length === 0 ? (
              <div className="py-8 text-center text-sm text-slate-500">
                {error ? `Disconnected: ${error}` : "Connecting to board through cloud gateway..."}
              </div>
            ) : (
              devices.map((device) => (
                <div
                  key={device.id}
                  className="flex items-center justify-between p-3 rounded-xl bg-slate-900/50 border border-slate-800/50 hover:border-slate-700 transition"
                >
                  <div className="flex items-center gap-3">
                    <div className="flex items-center justify-center w-9 h-9 rounded-lg bg-slate-800">
                      <Monitor className="w-4 h-4 text-slate-400" />
                    </div>
                    <div>
                      <p className="text-sm font-medium text-white">{device.name}</p>
                      <p className="text-xs text-slate-500">Cloud WebSocket · v{device.firmware}</p>
                    </div>
                  </div>
                  <div className="flex items-center gap-4">
                    <StorageBar
                      used={device.storage_used ?? 0}
                      total={device.storage_total ?? 7 * 1024 * 1024}
                    />
                    <StatusBadge online={device.online} />
                  </div>
                </div>
              ))
            )}
          </div>
        </Section>

        {/* ── Live Logs ────────────────────────────── */}
        <Section
          title="System Logs"
          action={
            <Link
              href="/logs"
              className="flex items-center gap-1 text-xs text-cyan-400 hover:text-cyan-300 transition"
            >
              Full log <ArrowUpRight className="w-3 h-3" />
            </Link>
          }
        >
          <div className="space-y-0.5 font-mono text-xs max-h-80 overflow-y-auto">
            {logs.length === 0 ? (
              <div className="py-8 text-center text-sm font-sans text-slate-500">
                {error
                  ? `Board disconnected (${error})`
                  : board
                    ? "Cloud connection active · device logs are available only on the local network"
                    : "Connecting to board through cloud gateway..."}
              </div>
            ) : (
              logs.map((log, i) => (
                <div
                  key={i}
                  className="flex gap-3 py-1.5 border-b border-slate-800/50 last:border-0"
                >
                  <span className="text-slate-600 w-16 flex-shrink-0">{log.ts}</span>
                  <span
                    className={`w-12 flex-shrink-0 font-semibold ${
                      log.level === "ERROR"
                        ? "text-red-400"
                        : log.level === "WARN"
                          ? "text-orange-400"
                          : "text-slate-500"
                    }`}
                  >
                    {log.level}
                  </span>
                  <span className="text-cyan-400/70 w-14 flex-shrink-0">{log.source}</span>
                  <span className="text-slate-300">{log.msg}</span>
                </div>
              ))
            )}
          </div>
        </Section>
      </div>

      {/* ── Quick Actions ──────────────────────────── */}
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4 animate-slide-up" style={{ animationDelay: "300ms" }}>
        {[
          { icon: Activity, label: "Check OTA", desc: "Scan for firmware updates", href: "/ota", color: "#06b6d4" },
          { icon: Thermometer, label: "Device Health", desc: "View CPU & memory stats", href: "/devices", color: "#10b981" },
          { icon: Clock, label: "Clock Style", desc: "Customize clock display", href: "/clock", color: "#a855f7" },
          { icon: HardDrive, label: "Manage Storage", desc: "LittleFS file browser", href: "/settings", color: "#f97316" },
        ].map((action) => (
          <Link
            key={action.label}
            href={action.href}
            className="dom-card p-5 flex items-center gap-4 no-underline group"
          >
            <div
              className="flex items-center justify-center w-10 h-10 rounded-xl transition-transform group-hover:scale-110"
              style={{ background: `${action.color}15` }}
            >
              <action.icon className="w-5 h-5" style={{ color: action.color }} />
            </div>
            <div>
              <p className="text-sm font-semibold text-white">{action.label}</p>
              <p className="text-xs text-slate-500">{action.desc}</p>
            </div>
          </Link>
        ))}
      </div>
    </>
  );
}
