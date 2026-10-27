import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

/**
 * Keep the operator-facing orchestrator responsive: replace every blocking
 * subagent wait_all call with an immediate, non-blocking list call.
 *
 * This hook also covers nested tool calls made through ctx.executeTool(),
 * including calls issued by codemode.
 */
export default function (pi: ExtensionAPI) {
  pi.on("tool_call", async (event) => {
    const input = event.input as Record<string, any>;

    if (event.toolName === "subagent" && input.action === "wait_all") {
      input.action = "list";
      delete input.timeoutSeconds;
      delete input.runIds;
    }
  });
}
