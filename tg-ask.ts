import { Plugin } from "@opencode/plugin"
import { spawn } from "node:child_process"
import { appendFileSync, existsSync } from "node:fs"
import { basename, dirname, join } from "node:path"

// Telegram ↔ OpenCode bridge.
//
// OpenCode questions (the built-in question tool creates a form, permission
// requests create permission.ask) go to Telegram; the answer from Telegram
// returns to the session: permissions via ctx.permission.reply, forms via
// the `opencode api` CLI.
//
// About forms specifically: ctx.session has no form (SessionDomain is a Pick
// without form), and client/schema packages aren't imported from the plugin.
// `opencode api` is the supported path to the same server.
//
// The script lives next to its own .env, while the plugin is global, so the
// path is overridden by the TG_ASK_SCRIPT environment variable.

// Script path: install.sh substitutes this folder's path at install time,
// the TG_ASK_SCRIPT environment variable overrides it.
const SCRIPT = process.env.TG_ASK_SCRIPT ?? "/absolute/path/to/tg-ask.py"
const PYTHON = process.env.TG_ASK_PYTHON ?? "python3"
const OPENCODE = process.env.TG_ASK_OPENCODE ?? "opencode"
const LOG = join(dirname(SCRIPT), "tg-ask.log")
const TIMEOUT_SEC = 900 // how long to wait for a Telegram reply
const GRACE_SEC = 60 // margin before forcibly ending the dialog
const TIMEOUT_NOTE = `No answer in Telegram within ${Math.round(TIMEOUT_SEC / 60)} minutes.`

type AskField = Record<string, unknown>
type AskResult = {
  ok: boolean
  reason?: string
  field?: string
  error?: string
  answers?: Record<string, unknown>
  raw?: string
}

type Dialog = { signal: AbortSignal; cancel: () => void; dispose: () => void }

function logEvent(fields: Record<string, unknown>): void {
  try {
    appendFileSync(LOG, `${JSON.stringify({ ts: Math.round(Date.now() / 100) / 10, ...fields })}\n`)
  } catch {
    // log is diagnostics only; its unavailability must not break the session
  }
}

function runProcess(
  command: string,
  args: string[],
  options: { stdin?: string; abort: AbortSignal; limitSec: number },
): Promise<{ code: number; stdout: string; stderr: string; note?: string }> {
  return new Promise((resolve) => {
    let child: ReturnType<typeof spawn>
    try {
      child = spawn(command, args, { stdio: ["pipe", "pipe", "pipe"] })
    } catch (error) {
      resolve({ code: -1, stdout: "", stderr: "", note: String(error) })
      return
    }
    let stdout = ""
    let stderr = ""
    let note: string | undefined
    let settled = false
    const finish = (code: number) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      options.abort.removeEventListener("abort", onAbort)
      resolve({ code, stdout, stderr, note })
    }
    const stop = (why: string) => {
      note = why
      child.kill("SIGTERM")
      setTimeout(() => child.kill("SIGKILL"), 3000).unref?.()
    }
    const onAbort = () => stop("aborted")
    const timer = setTimeout(() => stop("timeout"), (options.limitSec + GRACE_SEC) * 1000)
    timer.unref?.()
    if (options.abort.aborted) onAbort()
    else options.abort.addEventListener("abort", onAbort, { once: true })
    child.stdout?.on("data", (chunk) => {
      stdout += String(chunk)
    })
    child.stderr?.on("data", (chunk) => {
      stderr += String(chunk)
    })
    child.on("error", (error) => {
      note = String(error)
      finish(-1)
    })
    child.on("close", (code) => finish(code ?? -1))
    child.stdin?.on("error", () => {})
    child.stdin?.end(options.stdin ?? "")
  })
}

function dialogAbort(parent: AbortSignal): Dialog {
  const controller = new AbortController()
  const relay = () => controller.abort()
  if (parent.aborted) relay()
  else parent.addEventListener("abort", relay, { once: true })
  return {
    signal: controller.signal,
    cancel: relay,
    dispose: () => parent.removeEventListener("abort", relay),
  }
}

async function ask(request: Record<string, unknown>, signal: AbortSignal): Promise<AskResult> {
  const result = await runProcess(PYTHON, [SCRIPT, "ask"], {
    stdin: JSON.stringify({ timeout: TIMEOUT_SEC, ...request }),
    abort: signal,
    limitSec: TIMEOUT_SEC,
  })
  const line = result.stdout
    .split("\n")
    .map((item) => item.trim())
    .filter(Boolean)
    .pop()
  if (!line) {
    return { ok: false, reason: result.note ?? "error", error: result.note ?? result.stderr.trim() ?? `exit ${result.code}` }
  }
  try {
    return JSON.parse(line) as AskResult
  } catch {
    return { ok: false, reason: "error", error: `invalid script response: ${line.slice(0, 200)}` }
  }
}

async function opencodeApi(
  method: "post" | "delete",
  path: string,
  data: unknown,
  signal: AbortSignal,
): Promise<{ ok: boolean; error: string }> {
  const args = ["api", method, path]
  if (data !== undefined) args.push("--data", JSON.stringify(data))
  const result = await runProcess(OPENCODE, args, { abort: signal, limitSec: 60 })
  if (result.code !== 0) {
    return { ok: false, error: (result.stdout || result.stderr).trim().slice(0, 300) || `exit ${result.code}` }
  }
  return { ok: true, error: "" }
}

function toAskField(field: Record<string, unknown>): AskField | undefined {
  if (!field || typeof field !== "object" || field.hidden === true) return undefined
  const key = String(field.key ?? "")
  if (!key) return undefined
  const base = { key, title: String(field.title ?? "") }
  const type = String(field.type ?? "string")
  if (type === "external") return { ...base, type: "string", external: true, url: String(field.url ?? "") }
  const options = Array.isArray(field.options)
    ? field.options.map((option) => {
        const item = (option ?? {}) as Record<string, unknown>
        return {
          label: String(item.label ?? ""),
          value: item.value,
          description: String(item.description ?? ""),
        }
      })
    : undefined
  return {
    ...base,
    type,
    options,
    custom: field.custom,
    required: field.required,
    minItems: field.minItems,
    maxItems: field.maxItems,
    pattern: field.pattern,
    minLength: field.minLength,
    maxLength: field.maxLength,
  }
}

async function describeSession(ctx: any, sessionID: string): Promise<string> {
  const parts: string[] = []
  if (sessionID) {
    try {
      const info = await ctx.session.get({ sessionID })
      if (info?.title) parts.push(String(info.title))
    } catch (error) {
      logEvent({ kind: "session-get-failed", error: String(error) })
    }
  }
  if (ctx.location?.directory) parts.push(basename(ctx.location.directory))
  return parts.join(" · ")
}

export default Plugin.define({
  id: "tg-ask",
  async setup(ctx: any) {
    if (!existsSync(SCRIPT)) {
      logEvent({ kind: "disabled", reason: "no script", script: SCRIPT, location: ctx.location?.directory })
      return
    }
    const abort = new AbortController()
    const running = new Map<string, Dialog>()
    const settled = new Set<string>()

    const finish = (id: string) => {
      const dialog = running.get(id)
      if (!dialog) return
      settled.add(id)
      dialog.cancel()
    }

    const handleForm = async (form: any, directory: string) => {
      const id = String(form?.id ?? "")
      const sessionID = String(form?.sessionID ?? "")
      if (!id || !sessionID) return
      const keys = new Set<string>()
      const fields: AskField[] = []
      for (const raw of Array.isArray(form.fields) ? form.fields : []) {
        const field = toAskField(raw)
        if (!field) continue
        if (field.external !== true) keys.add(String(field.key))
        fields.push(field)
      }
      if (keys.size === 0) {
        logEvent({ kind: "form-skip", id, reason: "no fields to answer", directory })
        return
      }
      const title = String(form.title ?? "").trim() || "Question from OpenCode"
      const context = await describeSession(ctx, sessionID)
      logEvent({ kind: "form-ask", id, sessionID, directory, title, keys: [...keys] })
      const dialog = dialogAbort(abort.signal)
      running.set(id, dialog)
      let result: AskResult
      try {
        result = await ask({ text: title, context, fields }, dialog.signal)
      } finally {
        dialog.dispose()
        running.delete(id)
      }
      if (settled.delete(id)) {
        logEvent({ kind: "form-answered-in-tui", id })
        return
      }
      if (!result.ok) {
        logEvent({ kind: "form-timeout", id, reason: result.reason, error: result.error })
        const path = `/api/session/${encodeURIComponent(sessionID)}/form/${encodeURIComponent(id)}?message=${encodeURIComponent(TIMEOUT_NOTE)}`
        const cancelled = await opencodeApi("delete", path, undefined, abort.signal)
        if (!cancelled.ok) logEvent({ kind: "form-cancel-failed", id, error: cancelled.error })
        return
      }
      const answer: Record<string, unknown> = {}
      for (const [key, value] of Object.entries(result.answers ?? {})) {
        if (key.endsWith("_note") || !keys.has(key)) continue
        answer[key] = value
      }
      const path = `/api/session/${encodeURIComponent(sessionID)}/form/${encodeURIComponent(id)}/reply`
      const replied = await opencodeApi("post", path, { answer }, abort.signal)
      logEvent({
        kind: replied.ok ? "form-replied" : "form-reply-failed",
        id,
        answer,
        error: replied.error,
      })
    }

    const handlePermission = async (request: any, directory: string) => {
      const requestID = String(request?.id ?? "")
      const sessionID = String(request?.sessionID ?? "")
      if (!requestID || !sessionID) return
      const resources = (Array.isArray(request.resources) ? request.resources : []).map(String)
      const text = [
        `Permission required: ${request.action ?? "?"}`,
        request.message ? String(request.message) : "",
      ]
        .filter(Boolean)
        .join("\n\n")
      const code = resources.join("\n")
      const context = await describeSession(ctx, sessionID)
      logEvent({ kind: "permission-ask", id: requestID, sessionID, directory, action: request.action })
      const dialog = dialogAbort(abort.signal)
      running.set(requestID, dialog)
      let result: AskResult
      try {
        result = await ask(
          {
            text,
            code,
            context,
            fields: [
              {
                key: "decision",
                title: "Allow?",
                type: "string",
                custom: true,
                required: true,
                options: [
                  { label: "Allow once", value: "once" },
                  { label: "Allow always (remember)", value: "always" },
                  { label: "Reject", value: "reject" },
                ],
              },
            ],
          },
          dialog.signal,
        )
      } finally {
        dialog.dispose()
        running.delete(requestID)
      }
      if (settled.delete(requestID)) {
        logEvent({ kind: "permission-answered-in-tui", id: requestID })
        return
      }
      const raw = String(result.answers?.decision ?? "").trim()
      const note = String(result.answers?.decision_note ?? "").trim()
      let decision: "once" | "always" | "reject" = "reject"
      let message = TIMEOUT_NOTE
      if (result.ok && (raw === "once" || raw === "always" || raw === "reject")) {
        decision = raw
        message = decision === "reject" ? note || "Rejected via Telegram." : ""
      } else if (result.ok) {
        message = raw ? `Rejected via Telegram: ${raw}` : "Rejected via Telegram."
      }
      try {
        await ctx.permission.reply({ sessionID, requestID, decision, message: message || undefined })
        logEvent({ kind: "permission-replied", id: requestID, decision })
      } catch (error) {
        logEvent({ kind: "permission-reply-failed", id: requestID, decision, error: String(error) })
      }
    }

    try {
      await ctx.tool.transform((editor: any) => {
        editor.add({
          name: "ask_user_via_telegram",
          description:
            "Ask the user a clarifying question in Telegram and wait for an answer (up to 15 minutes). " +
            "Use when the user is away from the terminal and their answer is needed as text; " +
            "for choosing among several options the built-in question tool is better.",
          input: {
            type: "object",
            properties: {
              question: { type: "string", description: "Question text for the user" },
            },
            required: ["question"],
            additionalProperties: false,
          },
          execute: async (input: any, context: any) => {
            const question = String(input?.question ?? "").trim()
            if (!question) return { content: "Empty question — nothing to send to Telegram." }
            const context_ = await describeSession(ctx, String(context?.sessionID ?? ""))
            const result = await ask({ text: question, context: context_ }, context?.signal ?? abort.signal)
            if (!result.ok) {
              return {
                content: `User did not answer via Telegram (${result.reason ?? "error"}${result.error ? `: ${result.error}` : ""}).`,
              }
            }
            const answer = String(result.answers?.answer ?? result.raw ?? "")
            logEvent({ kind: "tool-answered", sessionID: context?.sessionID })
            return { content: `User's answer from Telegram:\n${answer}` }
          },
        })
      })
      logEvent({ kind: "tool-registered", script: SCRIPT, location: ctx.location?.directory })
    } catch (error) {
      logEvent({ kind: "tool-failed", error: String(error), location: ctx.location?.directory })
    }

    void (async () => {
      try {
        for await (const event of ctx.event.subscribe({ signal: abort.signal })) {
          const type = String(event?.type ?? "")
          const directory = event?.location?.directory
          if (directory && directory !== ctx.location?.directory) continue
          const data: any = event?.data ?? {}
          if (type === "form.created") {
            const id = String(data.form?.id ?? "")
            if (!id || running.has(id) || settled.has(id)) continue
            logEvent({ kind: "form-seen", id, directory })
            void handleForm(data.form, directory).catch((error) =>
              logEvent({ kind: "form-crashed", id, error: String(error) }),
            )
          } else if (type === "form.replied" || type === "form.cancelled") {
            finish(String(data.id ?? ""))
          } else if (type === "permission.asked") {
            const id = String(data.id ?? "")
            if (!id || running.has(id) || settled.has(id)) continue
            logEvent({ kind: "permission-seen", id, directory })
            void handlePermission(data, directory).catch((error) =>
              logEvent({ kind: "permission-crashed", id, error: String(error) }),
            )
          } else if (type === "permission.replied") {
            finish(String(data.requestID ?? ""))
          }
        }
      } catch (error) {
        if (!abort.signal.aborted) logEvent({ kind: "stream-failed", error: String(error) })
      }
    })()

    return () => {
      abort.abort()
      running.clear()
    }
  },
})