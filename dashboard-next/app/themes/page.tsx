"use client";

import { useState } from "react";
import { Check, Paintbrush, Type, Sun, Moon, Clock } from "lucide-react";
import { PageHeader, Section } from "@/components/dashboard-primitives";
import { Button } from "@/components/ui/button";
import { updateClockAppearance, type ClockAppearance } from "@/lib/board-api";
import { demoThemes } from "@/lib/demo-data";

const presetColors = [
  { name: "Cyan", value: "#06b6d4" },
  { name: "Blue", value: "#3b82f6" },
  { name: "Purple", value: "#a855f7" },
  { name: "Pink", value: "#ec4899" },
  { name: "Rose", value: "#f43f5e" },
  { name: "Orange", value: "#f97316" },
  { name: "Amber", value: "#f59e0b" },
  { name: "Green", value: "#10b981" },
  { name: "Teal", value: "#14b8a6" },
  { name: "Lime", value: "#84cc16" },
];

const fonts = ["Inter", "Roboto", "Outfit", "JetBrains Mono", "Space Grotesk", "DM Sans"];
const roundness = ["none", "small", "medium", "large", "full"];
const presetThemes = [
  { id: "cyan-dark", name: "Cyan Dark", primary_color: "#06b6d4", bg_color: "#050a18", font: "Inter", clock_style: "digital" },
  { id: "emerald-glow", name: "Emerald Glow", primary_color: "#10b981", bg_color: "#04140e", font: "Outfit", clock_style: "minimal" },
  { id: "purple-neon", name: "Purple Neon", primary_color: "#a855f7", bg_color: "#10061a", font: "JetBrains Mono", clock_style: "flip" },
  { id: "amber-sunset", name: "Amber Arc", primary_color: "#f59e0b", bg_color: "#180d04", font: "Space Grotesk", clock_style: "analog" },
];

const clockStylesList: Array<{ id: ClockAppearance["style"]; name: string; desc: string }> = [
  { id: "digital", name: "Digital", desc: "Classic bold numbers with divider & date" },
  { id: "minimal", name: "Minimal", desc: "Clean typography focused layout" },
  { id: "analog", name: "Futuristic Arc", desc: "Modern arc gauge & seconds ring" },
  { id: "flip", name: "Flip Clock", desc: "Retro split-flap card display" },
  { id: "word", name: "Word Clock", desc: "Spelled out text clock representation" },
  { id: "binary", name: "Binary Matrix", desc: "LED matrix bit-column dots" },
];

export default function ThemesPage() {
  const themes = demoThemes.length > 0 ? demoThemes : presetThemes;
  const [activeTheme, setActiveTheme] = useState(themes[0].id);
  const [primaryColor, setPrimaryColor] = useState("#06b6d4");
  const [selectedClockStyle, setSelectedClockStyle] = useState<ClockAppearance["style"]>("digital");
  const [selectedFont, setSelectedFont] = useState("Inter");
  const [selectedRoundness, setSelectedRoundness] = useState("medium");
  const [mode, setMode] = useState<"dark" | "light">("dark");
  const [is24Hour, setIs24Hour] = useState(true);
  const [showSeconds, setShowSeconds] = useState(true);
  const [showDate, setShowDate] = useState(true);
  const [applying, setApplying] = useState(false);
  const [statusMsg, setStatusMsg] = useState<string | null>(null);

  const syncToDevice = async (
    style: ClockAppearance["style"] = selectedClockStyle,
    color = primaryColor,
    currentMode = mode,
  ) => {
    setApplying(true);
    setStatusMsg(`Syncing clock ('${style}', ${color}, ${currentMode}) through the cloud...`);
    try {
      await updateClockAppearance({
        style,
        color,
        mode: currentMode,
      });
      setStatusMsg(`Synced through cloud: ${style.toUpperCase()} (${currentMode.toUpperCase()})`);
      setTimeout(() => setStatusMsg(null), 3000);
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : "Unknown device error";
      setStatusMsg(`Device Sync Error: ${message}`);
      setTimeout(() => setStatusMsg(null), 4000);
    } finally {
      setApplying(false);
    }
  };

  const handleApplyTheme = async () => {
    await syncToDevice(selectedClockStyle, primaryColor, mode);
  };

  return (
    <>
      <PageHeader
        title="Theme & Clock"
        subtitle="Customize look and feel of device and dashboard"
        badge="Design System"
        action={
          <div className="flex items-center gap-2">
            <Button className="gap-2" onClick={handleApplyTheme} disabled={applying}>
              <Paintbrush className="w-4 h-4" /> {applying ? "Applying..." : "Apply to Device"}
            </Button>
          </div>
        }
      />

      {statusMsg && (
        <div className={`p-4 rounded-xl mb-6 font-medium text-sm border animate-fade-in ${
          statusMsg.startsWith("Error") || statusMsg.startsWith("Device Sync Error")
            ? "bg-red-500/10 text-red-400 border-red-500/20"
            : "bg-cyan-500/10 text-cyan-400 border-cyan-500/20"
        }`}>
          {statusMsg}
        </div>
      )}

      {/* ── Section 1: Customize Clock ────────────────────────── */}
      <Section title="Customize Clock" className="mb-8">
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {clockStylesList.map((cs) => (
            <button
              key={cs.id}
              className={`flex items-start gap-4 p-4 rounded-xl border text-left transition ${
                selectedClockStyle === cs.id
                  ? "border-cyan-500/60 bg-cyan-500/10 shadow-lg shadow-cyan-500/5"
                  : "border-slate-800/60 bg-slate-900/40 hover:border-slate-700"
              }`}
              onClick={() => {
                setSelectedClockStyle(cs.id);
                syncToDevice(cs.id, primaryColor, mode);
              }}
            >
              <div
                className={`p-2.5 rounded-xl border flex-shrink-0 ${
                  selectedClockStyle === cs.id
                    ? "bg-cyan-500/20 border-cyan-500/40 text-cyan-300"
                    : "bg-slate-800/50 border-slate-700/50 text-slate-400"
                }`}
              >
                <Clock className="w-5 h-5" />
              </div>
              <div className="flex-1 min-w-0">
                <div className="flex items-center justify-between">
                  <p className="text-sm font-semibold text-white">{cs.name}</p>
                  {selectedClockStyle === cs.id && (
                    <Check className="w-4 h-4 text-cyan-400 flex-shrink-0" />
                  )}
                </div>
                <p className="text-xs text-slate-400 mt-1">{cs.desc}</p>
              </div>
            </button>
          ))}
        </div>

        {/* Time Format & Display Preferences */}
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 mt-6 pt-6 border-t border-slate-800/60">
          <div className="flex items-center justify-between p-3.5 rounded-xl bg-slate-900/30 border border-slate-800/60">
            <div>
              <p className="text-xs font-semibold text-white">Time Format</p>
              <p className="text-[11px] text-slate-400">Toggle 24h / 12h AM-PM</p>
            </div>
            <button
              className={`px-3 py-1.5 rounded-lg text-xs font-semibold transition border ${
                is24Hour ? "bg-cyan-500/20 text-cyan-300 border-cyan-500/40" : "bg-slate-800 text-slate-400 border-slate-700"
              }`}
              onClick={() => setIs24Hour(!is24Hour)}
            >
              {is24Hour ? "24 Hour" : "12 Hour"}
            </button>
          </div>

          <div className="flex items-center justify-between p-3.5 rounded-xl bg-slate-900/30 border border-slate-800/60">
            <div>
              <p className="text-xs font-semibold text-white">Show Seconds</p>
              <p className="text-[11px] text-slate-400">Display seconds counter</p>
            </div>
            <button
              className={`px-3 py-1.5 rounded-lg text-xs font-semibold transition border ${
                showSeconds ? "bg-cyan-500/20 text-cyan-300 border-cyan-500/40" : "bg-slate-800 text-slate-400 border-slate-700"
              }`}
              onClick={() => setShowSeconds(!showSeconds)}
            >
              {showSeconds ? "Enabled" : "Disabled"}
            </button>
          </div>

          <div className="flex items-center justify-between p-3.5 rounded-xl bg-slate-900/30 border border-slate-800/60">
            <div>
              <p className="text-xs font-semibold text-white">Show Date</p>
              <p className="text-[11px] text-slate-400">Display weekday & month</p>
            </div>
            <button
              className={`px-3 py-1.5 rounded-lg text-xs font-semibold transition border ${
                showDate ? "bg-cyan-500/20 text-cyan-300 border-cyan-500/40" : "bg-slate-800 text-slate-400 border-slate-700"
              }`}
              onClick={() => setShowDate(!showDate)}
            >
              {showDate ? "Enabled" : "Disabled"}
            </button>
          </div>
        </div>
      </Section>

      <div className="grid gap-6 xl:grid-cols-3 mb-8">
        {/* ── Saved Themes ─────────────────────────── */}
        <Section title="Preset Themes">
          <div className="space-y-3">
            {themes.map((theme) => (
              <button
                key={theme.id}
                className={`w-full flex items-center gap-4 p-4 rounded-xl border transition text-left ${
                  activeTheme === theme.id
                    ? "border-cyan-500/50 bg-cyan-500/5"
                    : "border-slate-800/50 bg-slate-900/30 hover:border-slate-700"
                }`}
                onClick={() => {
                  setActiveTheme(theme.id);
                  setPrimaryColor(theme.primary_color);
                  setSelectedFont(theme.font);
                  const st = theme.clock_style || selectedClockStyle;
                  if (theme.clock_style) setSelectedClockStyle(theme.clock_style);
                  syncToDevice(st, theme.primary_color, mode);
                }}
              >
                <div
                  className="w-10 h-10 rounded-xl flex-shrink-0 border border-white/10"
                  style={{
                    background: `linear-gradient(135deg, ${theme.primary_color} 0%, ${theme.bg_color} 100%)`,
                  }}
                />
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-semibold text-white">{theme.name}</p>
                  <p className="text-xs text-slate-500">
                    {theme.font} · {theme.clock_style}
                  </p>
                </div>
                {activeTheme === theme.id && (
                  <Check className="w-5 h-5 text-cyan-400 flex-shrink-0" />
                )}
              </button>
            ))}
          </div>
        </Section>

        {/* ── Color Palette ────────────────────────── */}
        <Section title="Color Palette">
          <div className="space-y-5">
            <div>
              <p className="text-xs text-slate-400 mb-3 font-medium">Primary Accent Color</p>
              <div className="grid grid-cols-5 gap-3">
                {presetColors.map((c) => (
                  <button
                    key={c.value}
                    className={`group relative w-full aspect-square rounded-xl border-2 transition-all hover:scale-110 ${
                      primaryColor === c.value
                        ? "border-white shadow-lg"
                        : "border-transparent"
                    }`}
                    style={{ background: c.value }}
                    onClick={() => {
                      setPrimaryColor(c.value);
                      syncToDevice(selectedClockStyle, c.value, mode);
                    }}
                    title={c.name}
                  >
                    {primaryColor === c.value && (
                      <Check className="absolute inset-0 m-auto w-4 h-4 text-white drop-shadow" />
                    )}
                  </button>
                ))}
              </div>
            </div>

            <div>
              <p className="text-xs text-slate-400 mb-3 font-medium">Custom Color Hex</p>
              <div className="flex items-center gap-3">
                <input
                  type="color"
                  value={primaryColor}
                  onChange={(e) => {
                    setPrimaryColor(e.target.value);
                    syncToDevice(selectedClockStyle, e.target.value, mode);
                  }}
                  className="w-10 h-10 rounded-lg cursor-pointer border-0 bg-transparent"
                />
                <input
                  type="text"
                  value={primaryColor}
                  onChange={(e) => setPrimaryColor(e.target.value)}
                  onBlur={() => syncToDevice(selectedClockStyle, primaryColor, mode)}
                  className="flex-1 px-3 py-2 rounded-lg bg-slate-900 border border-slate-700 text-sm text-white font-mono focus:border-cyan-500 focus:outline-none"
                />
              </div>
            </div>

            <div>
              <p className="text-xs text-slate-400 mb-3 font-medium">Appearance Theme</p>
              <div className="grid grid-cols-2 gap-3">
                <button
                  className={`flex items-center justify-center gap-2 p-3 rounded-xl border transition ${
                    mode === "dark"
                      ? "border-cyan-500/50 bg-cyan-500/5 text-white"
                      : "border-slate-800 text-slate-400 hover:border-slate-700"
                  }`}
                  onClick={() => {
                    setMode("dark");
                    syncToDevice(selectedClockStyle, primaryColor, "dark");
                  }}
                >
                  <Moon className="w-4 h-4" />
                  <span className="text-sm font-medium">Dark</span>
                </button>
                <button
                  className={`flex items-center justify-center gap-2 p-3 rounded-xl border transition ${
                    mode === "light"
                      ? "border-cyan-500/50 bg-cyan-500/5 text-white"
                      : "border-slate-800 text-slate-400 hover:border-slate-700"
                  }`}
                  onClick={() => {
                    setMode("light");
                    syncToDevice(selectedClockStyle, primaryColor, "light");
                  }}
                >
                  <Sun className="w-4 h-4" />
                  <span className="text-sm font-medium">Light</span>
                </button>
              </div>
            </div>
          </div>
        </Section>

        {/* ── Typography & Shape ───────────────────── */}
        <div className="space-y-6">
          <Section title="Typography">
            <div className="space-y-2">
              {fonts.map((font) => (
                <button
                  key={font}
                  className={`w-full flex items-center justify-between p-3 rounded-xl border transition text-left ${
                    selectedFont === font
                      ? "border-cyan-500/50 bg-cyan-500/5"
                      : "border-slate-800/50 hover:border-slate-700"
                  }`}
                  onClick={() => setSelectedFont(font)}
                >
                  <div className="flex items-center gap-3">
                    <Type className="w-4 h-4 text-slate-500" />
                    <span className="text-sm text-white" style={{ fontFamily: font }}>
                      {font}
                    </span>
                  </div>
                  {selectedFont === font && <Check className="w-4 h-4 text-cyan-400" />}
                </button>
              ))}
            </div>
          </Section>

          <Section title="Corner Roundness">
            <div className="grid grid-cols-5 gap-2">
              {roundness.map((r) => {
                const radiusMap: Record<string, string> = {
                  none: "0px",
                  small: "4px",
                  medium: "12px",
                  large: "20px",
                  full: "999px",
                };
                return (
                  <button
                    key={r}
                    className={`flex flex-col items-center gap-2 p-3 rounded-lg border transition ${
                      selectedRoundness === r
                        ? "border-cyan-500/50 bg-cyan-500/5"
                        : "border-slate-800/50 hover:border-slate-700"
                    }`}
                    onClick={() => setSelectedRoundness(r)}
                  >
                    <div
                      className="w-8 h-8 border-2 border-cyan-400"
                      style={{ borderRadius: radiusMap[r] }}
                    />
                    <span className="text-[10px] text-slate-500 capitalize">{r}</span>
                  </button>
                );
              })}
            </div>
          </Section>
        </div>
      </div>

      {/* ── Live Preview ─────────────────────────────────── */}
      <Section title="Device Screen Live Preview">
        <div className="flex items-center justify-center py-8">
          <div
            className="w-80 h-60 rounded-2xl border border-white/20 shadow-2xl overflow-hidden relative transition-colors duration-300"
            style={{ background: mode === "dark" ? "#000000" : "#f1f5f9" }}
          >
            {/* Header label */}
            <div className="absolute top-2.5 right-3">
              <span className={`text-[10px] font-bold tracking-wide ${mode === "dark" ? "text-slate-500" : "text-slate-400"}`}>
                DomOS
              </span>
            </div>

            {/* Dynamic Clock Render based on selectedClockStyle */}
            <div className="absolute inset-0 flex flex-col items-center justify-center p-4">
              {selectedClockStyle === "digital" && (
                <>
                  <p
                    className="text-5xl font-extrabold tracking-tight"
                    style={{ color: primaryColor, fontFamily: selectedFont }}
                  >
                    13:28
                  </p>
                  <div className="w-12 h-0.5 my-2 rounded" style={{ background: primaryColor }} />
                  {showDate && (
                    <p className={`text-xs font-semibold tracking-wider ${mode === "dark" ? "text-slate-400" : "text-slate-600"}`}>
                      SUN · AUG 9
                    </p>
                  )}
                </>
              )}

              {selectedClockStyle === "minimal" && (
                <>
                  <p
                    className="text-5xl font-bold tracking-normal"
                    style={{ color: primaryColor, fontFamily: selectedFont }}
                  >
                    13:28
                  </p>
                  {showDate && (
                    <p className={`text-xs font-medium mt-2 tracking-widest ${mode === "dark" ? "text-slate-400" : "text-slate-600"}`}>
                      SUNDAY
                    </p>
                  )}
                </>
              )}

              {selectedClockStyle === "analog" && (
                <div className="relative w-36 h-36 flex items-center justify-center">
                  <svg className="absolute inset-0 w-full h-full transform -rotate-90">
                    <circle cx="72" cy="72" r="60" stroke={mode === "dark" ? "#1e293b" : "#cbd5e1"} strokeWidth="6" fill="none" />
                    <circle cx="72" cy="72" r="60" stroke={primaryColor} strokeWidth="6" strokeDasharray="377" strokeDashoffset="120" strokeLinecap="round" fill="none" />
                  </svg>
                  <div className="text-center z-10">
                    <p className="text-2xl font-bold" style={{ color: primaryColor, fontFamily: selectedFont }}>
                      13:28
                    </p>
                    <p className="text-[10px] font-semibold text-slate-400">45 SEC</p>
                  </div>
                </div>
              )}

              {selectedClockStyle === "flip" && (
                <div className="flex flex-col items-center gap-2">
                  <div className="flex items-center gap-1.5">
                    {["1", "3", ":", "2", "8"].map((char, i) => (
                      char === ":" ? (
                        <span key={i} className="text-2xl font-bold animate-pulse" style={{ color: primaryColor }}>:</span>
                      ) : (
                        <div
                          key={i}
                          className={`w-10 h-14 rounded-lg flex items-center justify-center border shadow-md ${
                            mode === "dark" ? "bg-slate-900 border-slate-800" : "bg-white border-slate-200"
                          }`}
                        >
                          <span className="text-2xl font-bold" style={{ color: primaryColor, fontFamily: selectedFont }}>
                            {char}
                          </span>
                        </div>
                      )
                    ))}
                  </div>
                  {showDate && (
                    <p className={`text-xs font-medium mt-1 ${mode === "dark" ? "text-slate-400" : "text-slate-600"}`}>
                      Sun, Aug 9
                    </p>
                  )}
                </div>
              )}

              {selectedClockStyle === "word" && (
                <div className="text-center leading-snug space-y-0.5">
                  <p className="text-xs font-semibold text-slate-500">IT IS</p>
                  <p className="text-lg font-extrabold tracking-wider" style={{ color: primaryColor }}>
                    TWENTY EIGHT
                  </p>
                  <p className="text-xs font-semibold text-slate-500">PAST</p>
                  <p className="text-lg font-extrabold tracking-wider" style={{ color: primaryColor }}>
                    ONE
                  </p>
                </div>
              )}

              {selectedClockStyle === "binary" && (
                <div className="flex items-center justify-center gap-8 py-2">
                  <div className="grid grid-cols-5 gap-1.5">
                    {[1, 0, 1, 1, 0].map((on, i) => (
                      <div key={i} className="w-3.5 h-3.5 rounded-sm" style={{ background: on ? primaryColor : (mode === "dark" ? "#1e293b" : "#cbd5e1") }} />
                    ))}
                  </div>
                  <div className="grid grid-cols-6 gap-1.5">
                    {[0, 1, 1, 1, 0, 0].map((on, i) => (
                      <div key={i} className="w-3.5 h-3.5 rounded-sm" style={{ background: on ? primaryColor : (mode === "dark" ? "#1e293b" : "#cbd5e1") }} />
                    ))}
                  </div>
                </div>
              )}
            </div>

            {/* Bottom-left Menu button matching device UI */}
            <div className="absolute bottom-2 left-2">
              <div
                className={`w-10 h-5 rounded flex items-center justify-center text-[10px] font-bold border ${
                  mode === "dark" ? "bg-slate-800 text-slate-200 border-slate-700" : "bg-slate-200 text-slate-800 border-slate-300"
                }`}
              >
                ☰
              </div>
            </div>

            {/* Screen bezel label */}
            <div className="absolute bottom-1 right-2 text-right">
              <span className="text-[8px] text-slate-500 font-mono">ES3C28P · 2.8&quot;</span>
            </div>
          </div>
        </div>
      </Section>
    </>
  );
}
