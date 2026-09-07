"use client";

import { useState, useRef, useCallback, useEffect } from "react";
import { Image, Trash2, Send, CheckCircle2, CloudUpload } from "lucide-react";
import { PageHeader, EmptyState } from "@/components/dashboard-primitives";
import { Button } from "@/components/ui/button";
import { setBoardWallpaper, syncBoardWallpapers } from "@/lib/board-api";
import { demoWallpapers } from "@/lib/demo-data";
import {
  deleteCloudWallpaper,
  fetchCloudWallpapers,
  uploadCloudWallpaper,
} from "@/lib/wallpaper-api";
import type { Wallpaper } from "@/lib/api";

export default function WallpaperPage() {
  const [wallpapers, setWallpapers] = useState<Wallpaper[]>(demoWallpapers);
  const [selected, setSelected] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const [pushing, setPushing] = useState(false);
  const [pushStatus, setPushStatus] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const loadWallpapersFromBackend = useCallback(async (): Promise<Wallpaper[] | null> => {
    try {
      return await fetchCloudWallpapers();
    } catch (e) {
      console.warn("Cloud wallpaper API unavailable:", e);
      setPushStatus("Cloud backend is unavailable. Please try again shortly.");
    }
    return null;
  }, []);

  useEffect(() => {
    let active = true;
    void loadWallpapersFromBackend().then((list) => {
      if (active && list) setWallpapers(list);
    });
    return () => {
      active = false;
    };
  }, [loadWallpapersFromBackend]);

  const handlePushToDevice = async (wp?: Wallpaper) => {
    const target = wp || wallpapers.find((w) => w.id === selected);
    if (!target) return;

    setPushing(true);
    setPushStatus(`Sending '${target.filename}' through the cloud gateway...`);
    try {
      await setBoardWallpaper(target.id);
      setPushStatus(`Wallpaper '${target.filename}' queued on the connected board.`);
      setTimeout(() => setPushStatus(null), 4000);
    } catch (err: unknown) {
      const errorMessage = err instanceof Error ? err.message : "Unknown device error";
      setPushStatus(`Device Sync Error: ${errorMessage}`);
      setTimeout(() => setPushStatus(null), 7000);
    } finally {
      setPushing(false);
    }
  };

  const handleFiles = useCallback(async (files: FileList | null) => {
    if (!files) return;
    const fileList = Array.from(files).filter((f) => f.type.startsWith("image/"));
    for (const file of fileList) {
      try {
        await uploadCloudWallpaper(file);
        const refreshed = await loadWallpapersFromBackend();
        if (refreshed) setWallpapers(refreshed);
        await syncBoardWallpapers();
        setPushStatus(`Uploaded and synced '${file.name}' through the cloud gateway.`);
        setTimeout(() => setPushStatus(null), 3500);
      } catch (error) {
        setPushStatus(`Upload Error: ${error instanceof Error ? error.message : "Unknown error"}`);
      }
    }
  }, [loadWallpapersFromBackend]);

  const handleDelete = async (id: string) => {
    try {
      await deleteCloudWallpaper(id);
      await syncBoardWallpapers();
    } catch (e) {
      setPushStatus(`Delete Error: ${e instanceof Error ? e.message : "Unknown error"}`);
      return;
    }
    setWallpapers((prev) => prev.filter((w) => w.id !== id));
    if (selected === id) setSelected(null);
  };

  return (
    <>
      <PageHeader
        title="Wallpaper Gallery"
        subtitle="Go Backend resizes to 320x240 JPEG Q85 • Tap Screen Slideshow Button for 5s Loop"
        badge={`${wallpapers.length} images`}
        action={
          <div className="flex flex-wrap items-center gap-2">
            {selected && (
              <Button className="gap-2" onClick={() => handlePushToDevice()} disabled={pushing}>
                <Send className="w-4 h-4" /> {pushing ? "Applying..." : "Set on Device"}
              </Button>
            )}
          </div>

        }
      />




      {pushStatus && (
        <div className={`p-4 rounded-xl mb-6 font-medium text-sm border animate-fade-in ${
          pushStatus.startsWith("Error") || pushStatus.startsWith("Upload Error") || pushStatus.startsWith("Device Sync Error") || pushStatus.startsWith("Slideshow Command Error")
            ? "bg-red-500/10 text-red-400 border-red-500/20"
            : "bg-cyan-500/10 text-cyan-400 border-cyan-500/20"
        }`}>
          {pushStatus}
        </div>
      )}

      {/* Upload Dropzone */}
      <div
        className={`dom-dropzone mb-8 animate-fade-in ${dragging ? "dragging" : ""}`}
        onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => { e.preventDefault(); setDragging(false); handleFiles(e.dataTransfer.files); }}
        onClick={() => inputRef.current?.click()}
      >
        <input
          ref={inputRef}
          type="file"
          accept="image/*"
          multiple
          className="hidden"
          onChange={(e) => handleFiles(e.target.files)}
        />
        <div className="w-12 h-12 rounded-2xl bg-cyan-500/10 border border-cyan-500/20 text-cyan-400 flex items-center justify-center mb-3">
          <CloudUpload className="w-6 h-6" />
        </div>
        <p className="text-sm font-semibold text-white">Upload Wallpapers to Webserver Database</p>
        <p className="text-xs text-slate-500 mt-1">
          Backend automatically resizes uploaded 4K images to 320x240 JPEG Q85 + 107x80 thumbnail.
        </p>
        <p className="text-[11px] text-cyan-400/80 mt-2 font-medium">
          ESP32 512KB SRAM Optimized: Zero RGB RAM buffer, streamed directly to ILI9341 LCD.
        </p>
      </div>

      {/* Wallpapers Grid */}
      {wallpapers.length === 0 ? (
        <EmptyState
          icon={Image}
          title="No Wallpapers Available"
          description="Upload JPEG/PNG images above to save them in the webserver database."
        />
      ) : (
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4">
          {wallpapers.map((wp) => (
            <div
              key={wp.id}
              className={`group relative rounded-2xl border overflow-hidden transition-all duration-300 ${
                selected === wp.id
                  ? "border-cyan-500 bg-cyan-500/5 shadow-xl shadow-cyan-500/10 ring-2 ring-cyan-500/20"
                  : "border-slate-800/80 bg-slate-900/40 hover:border-slate-700"
              }`}
              onClick={() => {
                setSelected(wp.id);
                handlePushToDevice(wp);
              }}
            >
              {/* Thumbnail Container */}
              <div className="aspect-[4/3] bg-slate-950 relative overflow-hidden flex items-center justify-center">
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img
                  src={wp.thumbnail_url || wp.url}
                  alt={wp.filename}
                  className="w-full h-full object-cover group-hover:scale-105 transition-transform duration-500"
                />

                {/* Selected Overlay Checkmark */}
                {selected === wp.id && (
                  <div className="absolute inset-0 bg-cyan-950/40 backdrop-blur-[2px] flex items-center justify-center">
                    <CheckCircle2 className="w-10 h-10 text-cyan-400 drop-shadow-md animate-scale-in" />
                  </div>
                )}
              </div>

              {/* Card Footer */}
              <div className="p-3 flex items-center justify-between">
                <div className="min-w-0 flex-1">
                  <p className="text-xs font-semibold text-white truncate">{wp.filename}</p>
                  <p className="text-[10px] text-cyan-400 mt-0.5 font-medium">
                    320×240 px • JPEG Q85
                  </p>
                </div>
                <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                  <button
                    className="p-1.5 rounded-lg text-slate-400 hover:text-red-400 hover:bg-red-500/10 transition"
                    onClick={(e) => {
                      e.stopPropagation();
                      handleDelete(wp.id);
                    }}
                    title="Delete wallpaper from database"
                  >
                    <Trash2 className="w-3.5 h-3.5" />
                  </button>
                  <button
                    className="p-1.5 rounded-lg text-cyan-400 hover:bg-cyan-500/10 transition"
                    onClick={(e) => {
                      e.stopPropagation();
                      handlePushToDevice(wp);
                    }}
                    title="Stream wallpaper to device"
                  >
                    <Send className="w-3.5 h-3.5" />
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </>
  );
}
