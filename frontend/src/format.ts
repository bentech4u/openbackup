export function bytes(n: number | null | undefined, digits = 1): string {
  if (n == null) return "—";
  const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(i === 0 ? 0 : digits)} ${units[i]}`;
}

export function dateTime(s: string | null | undefined): string {
  if (!s) return "—";
  return new Date(s).toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function relative(s: string | null | undefined): string {
  if (!s) return "—";
  const diff = (new Date(s).getTime() - Date.now()) / 1000;
  const abs = Math.abs(diff);
  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  if (abs < 60) return rtf.format(Math.round(diff), "second");
  if (abs < 3600) return rtf.format(Math.round(diff / 60), "minute");
  if (abs < 86400) return rtf.format(Math.round(diff / 3600), "hour");
  return rtf.format(Math.round(diff / 86400), "day");
}

export function duration(start: string | null, end: string | null): string {
  if (!start) return "—";
  const secs = Math.max(0, ((end ? new Date(end) : new Date()).getTime() - new Date(start).getTime()) / 1000);
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = Math.floor(secs % 60);
  return h ? `${h}h ${m}m` : m ? `${m}m ${s}s` : `${s}s`;
}

export function throughput(bytesDone: number, start: string | null, end: string | null): string {
  if (!start || !bytesDone) return "—";
  const secs = ((end ? new Date(end) : new Date()).getTime() - new Date(start).getTime()) / 1000;
  return secs > 0 ? `${bytes(bytesDone / secs)}/s` : "—";
}

// Describe the common cron shapes in words; fall back to the expression.
export function describeCron(cron: string | null): string {
  if (!cron) return "Manual only";
  const p = cron.trim().split(/\s+/);
  if (p.length !== 5) return cron;
  const [min, hour, dom, mon, dow] = p;
  const t = /^\d+$/.test(min) && /^\d+$/.test(hour) ? `${hour.padStart(2, "0")}:${min.padStart(2, "0")}` : null;
  const days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  if (t && dom === "*" && mon === "*" && dow === "*") return `Daily at ${t}`;
  if (t && dom === "*" && mon === "*" && dow === "1-5") return `Weekdays at ${t}`;
  if (t && dom === "*" && mon === "*" && /^[0-6](,[0-6])*$/.test(dow))
    return `${dow.split(",").map((d) => days[+d]).join(", ")} at ${t}`;
  if (t && /^\d+$/.test(dom) && mon === "*" && dow === "*") return `Monthly on day ${dom} at ${t}`;
  if (/^\d+$/.test(min) && hour === "*" && dom === "*") return `Hourly at :${min.padStart(2, "0")}`;
  return cron;
}
