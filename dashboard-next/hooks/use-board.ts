"use client";

import { useEffect, useState } from "react";
import type { Device } from "@/lib/api";
import { fetchBoardStatus, type BoardStatus } from "@/lib/board-api";

export interface LogItem {
  ts: string;
  level: string;
  source: string;
  msg: string;
}

export function useBoard() {
  const [board, setBoard] = useState<BoardStatus | null>(null);
  const [logs, setLogs] = useState<LogItem[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [telemetry, setTelemetry] = useState<Array<{ time: string; heap: number }>>([]);


  useEffect(() => {
    let isMounted = true;

    async function fetchStatus() {
      try {
        const data = await fetchBoardStatus(AbortSignal.timeout(8_000));
        if (!data.online) throw new Error("Board is not connected to the cloud gateway");

        if (isMounted) {
          setBoard(data);
          setError(null);
          setLoading(false);

          if (typeof data.free_heap === "number") {
            const nowTime = new Date().toLocaleTimeString([], {
              hour: "2-digit",
              minute: "2-digit",
              second: "2-digit",
            });
            setTelemetry((prev) => [...prev, { time: nowTime, heap: data.free_heap! }].slice(-20));
          }
        }
      } catch (err: unknown) {
        if (isMounted) {
          setError(err instanceof Error ? err.message : "Failed to connect to board");
          setBoard(null);
          setLoading(false);
        }
      }
    }

    fetchStatus();
    const interval = setInterval(fetchStatus, 10_000);

    return () => {
      isMounted = false;
      clearInterval(interval);
    };
  }, []);

  const deviceList: Device[] = board
    ? [
        {
          id: board.id || "es3c28p-01",
          name: board.name || "ES3C28P Desk Terminal",
          mac: board.mac || "B8:1F:3F:C3:97:54",
          ip: "Cloud gateway",
          firmware: board.firmware || "0.2.1",
          online: board.online,
          last_seen: "Just now",
          storage_used: board.storage_used ?? 0,
          storage_total: board.storage_total ?? 7 * 1024 * 1024,
        },
      ]
    : [];


  const clearLogs = () => setLogs([]);

  return { board, deviceList, logs, clearLogs, telemetry, loading, error };
}
