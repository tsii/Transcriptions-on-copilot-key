import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

/**
 * Harte Lastschutz-Regel (Betreiber-Fall 28.10.: wiederholte pytest-Laeufe
 * eines Delegierten legten alle 8 Kerne lahm und verdreifachten die
 * Diktat-Latenz des STT-Dienstes).
 *
 * Jeder Bash-/PowerShell-Befehl, der pytest ausfuehrt, bekommt automatisch
 * `nice -n 19` + `OMP_NUM_THREADS=4` vorangestellt, sofern nicht bereits
 * `nice` darin vorkommt. Der Diktier-Dienst des Betreibers haelt damit
 * immer den CPU-Vorrang.
 */
function guardPytest(cmd: string): string | null {
  if (!/\bpytest\b/.test(cmd)) return null;
  if (/\bnice\b/.test(cmd)) return null;
  // Pro Segment (Zeilen, &&, ||, ;) praefixen, damit Schleifen wie
  // `for i in 1 2; do pytest; done` ebenfalls geschuetzt sind.
  const segs = cmd.split(/(\n|&&|\|\|)/);
  const fixed = segs
    .map((seg) => {
      const m = seg.match(/^(\s*)(.*)$/s);
      if (!m) return seg;
      const [, ws, rest] = m;
      if (!/\bpytest\b/.test(rest) || /\bnice\b/.test(rest)) return seg;
      if (!rest) return seg;
      return `${ws}nice -n 19 env OMP_NUM_THREADS=4 ${rest}`;
    })
    .join("");
  return fixed === cmd ? null : fixed;
}

export default function (pi: ExtensionAPI) {
  pi.on("tool_call", async (event) => {
    const name = String((event as any).toolName ?? "");
    if (name !== "bash" && name !== "powershell" && name !== "powershell") return;
    const input = event.input as Record<string, any>;
    const cmd = input?.command;
    if (typeof cmd !== "string") return;
    const fixed = guardPytest(cmd);
    if (fixed !== null) {
      event.input = { ...input, command: fixed };
    }
  });
}
