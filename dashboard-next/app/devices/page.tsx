"use client";

import { Monitor, Cloud, HardDrive, Clock, RefreshCw } from "lucide-react";
import { PageHeader, StatusBadge, StorageBar } from "@/components/dashboard-primitives";
import { Button } from "@/components/ui/button";
import { useBoard } from "@/hooks/use-board";

import { useState } from "react";

export default function DevicesPage() {
  const { deviceList, error } = useBoard();

  const [refreshing, setRefreshing] = useState(false);
  const devices = deviceList;

  const handleRefresh = async () => {
    setRefreshing(true);
    await new Promise((r) => setTimeout(r, 500));
    window.location.reload();
  };

  return (
    <>
      <PageHeader
        title="Devices"
        subtitle="Fleet Management"
        badge={`${devices.length} active`}
        action={
          <Button variant="outline" className="gap-2" onClick={handleRefresh} disabled={refreshing}>
            <RefreshCw className={`w-4 h-4 ${refreshing ? "animate-spin" : ""}`} /> {refreshing ? "Refreshing..." : "Refresh"}
          </Button>
        }
      />


      <div className="grid gap-6">
        {devices.length === 0 ? (
          <div className="dom-card p-12 text-center text-slate-500 text-sm">
            {error ? `Connection error: ${error}` : "Connecting to ES3C28P through cloud gateway..."}
          </div>
        ) : (
          devices.map((device, i) => (
          <div
            key={device.id}
            className="dom-card p-6 animate-slide-up"
            style={{ animationDelay: `${i * 80}ms` }}
          >
            <div className="flex flex-col lg:flex-row lg:items-center justify-between gap-6">
              {/* Device Info */}
              <div className="flex items-start gap-4">
                <div className="flex items-center justify-center w-14 h-14 rounded-2xl bg-gradient-to-br from-cyan-500/10 to-blue-600/10 border border-cyan-500/20">
                  <Monitor className="w-7 h-7 text-cyan-400" />
                </div>
                <div>
                  <div className="flex items-center gap-3 mb-1">
                    <h3 className="text-lg font-semibold text-white">{device.name}</h3>
                    <StatusBadge online={device.online} />
                  </div>
                  <p className="text-sm text-slate-500">{device.mac}</p>
                </div>
              </div>

              {/* Stats Grid */}
              <div className="grid grid-cols-2 lg:grid-cols-4 gap-6">
                <div>
                  <div className="flex items-center gap-1.5 text-slate-500 mb-1">
                    <Cloud className="w-3.5 h-3.5" />
                    <span className="text-xs">Connection</span>
                  </div>
                  <p className="text-sm font-medium text-white">Cloud gateway</p>
                  <p className="text-xs text-emerald-400">WebSocket active</p>
                </div>
                <div>
                  <div className="flex items-center gap-1.5 text-slate-500 mb-1">
                    <HardDrive className="w-3.5 h-3.5" />
                    <span className="text-xs">Storage</span>
                  </div>
                  <div className="mt-1">
                    <StorageBar
                      used={device.storage_used ?? 0}
                      total={device.storage_total ?? 7 * 1024 * 1024}
                    />
                  </div>
                </div>
                <div>
                  <div className="flex items-center gap-1.5 text-slate-500 mb-1">
                    <Monitor className="w-3.5 h-3.5" />
                    <span className="text-xs">Firmware</span>
                  </div>
                  <p className="text-sm font-medium text-white">v{device.firmware}</p>
                  <p className="text-xs text-slate-600">
                    {device.firmware === "0.2.1" ? (
                      <span className="text-emerald-400">Up to date</span>
                    ) : (
                      <span className="text-orange-400">Update available</span>
                    )}
                  </p>
                </div>
                <div>
                  <div className="flex items-center gap-1.5 text-slate-500 mb-1">
                    <Clock className="w-3.5 h-3.5" />
                    <span className="text-xs">Last Seen</span>
                  </div>
                  <p className="text-sm font-medium text-white">
                    {device.online
                      ? "Now"
                      : new Date(device.last_seen).toLocaleTimeString([], {
                          hour: "2-digit",
                          minute: "2-digit",
                        })}
                  </p>
                </div>
              </div>
            </div>

            {/* Actions */}
            <div className="flex gap-2 mt-5 pt-5 border-t border-slate-800/50">
              <Button variant="outline" className="text-xs h-8 px-3">
                Terminal
              </Button>
              <Button variant="outline" className="text-xs h-8 px-3">
                Reboot
              </Button>
              <Button variant="outline" className="text-xs h-8 px-3">
                Push Theme
              </Button>
              <Button variant="outline" className="text-xs h-8 px-3">
                Push Wallpaper
              </Button>
              <Button variant="outline" className="text-xs h-8 px-3">
                OTA Update
              </Button>
            </div>
          </div>
        ))
      )}
      </div>
    </>
  );
}
