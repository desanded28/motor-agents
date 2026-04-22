"""Shared Gemini tool-calling loop used by all three agents.

Benefits:
- One place to fix bugs / tune behavior for all agents
- Consistent tracing (every tool call recorded to trace JSON)
- Consistent colored CLI output
- Pluggable callback so the web UI can stream events to the browser via SSE
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Callable

from google import genai
from google.genai import types

from utils import cli
from utils.trace import Tracer

# Default model. Override with env var GEMINI_MODEL to switch tiers.
# Current Gemini 2.5 family (2.0 is deprecated for new users as of 2026):
#   gemini-2.5-flash-lite  ← default; lightest tier, least likely to hit capacity
#   gemini-2.5-flash       ← stronger reasoning, sometimes 503s under spikes
#   gemini-2.5-pro         ← strongest, slower, more contended
DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")


EventCallback = Callable[[dict], None]


def run_tool_loop(
    *,
    agent_name: str,
    system_instruction: str,
    function_declarations: list[dict],
    tool_impls: dict[str, Callable],
    user_input: str,
    max_iters: int = 15,
    model: str = DEFAULT_MODEL,
    temperature: float = 0.2,
    verbose: bool = True,
    on_event: EventCallback | None = None,
) -> tuple[str, str]:
    """Run a Gemini tool-calling loop until the model produces a final text response.

    Returns (final_text, trace_path). Always saves a trace file, even on error.
    """
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise SystemExit("Set GEMINI_API_KEY in bmw-agents/.env (see .env.example)")

    tracer = Tracer.start(agent=agent_name, input=user_input, model=model)

    def _emit(event_type: str, payload: dict) -> None:
        if on_event:
            try:
                on_event({"type": event_type, "ts": time.time(), **payload})
            except Exception:
                pass

    _emit("start", {"agent": agent_name, "input": user_input})

    client = genai.Client(api_key=api_key)
    tools = [types.Tool(function_declarations=function_declarations)]
    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        tools=tools,
        temperature=temperature,
    )

    contents: list[types.Content] = [
        types.Content(role="user", parts=[types.Part(text=user_input)])
    ]

    def _generate_with_retry(contents_arg, max_retries: int = 3):
        """Call Gemini with retry-on-transient (503/UNAVAILABLE/429). Exponential backoff."""
        last_err: Exception | None = None
        for attempt in range(max_retries):
            try:
                return client.models.generate_content(model=model, contents=contents_arg, config=config)
            except Exception as e:
                msg = str(e)
                transient = any(x in msg for x in ("503", "UNAVAILABLE", "overloaded", "high demand", "429"))
                last_err = e
                if not transient or attempt == max_retries - 1:
                    raise
                wait = 2 ** attempt + 0.5  # 1.5, 2.5, 4.5 seconds
                if verbose:
                    print(cli.c(f"  [transient Gemini error — retry in {wait:.1f}s]", "yellow"))
                _emit("retry", {"error": msg[:160], "wait_s": wait})
                time.sleep(wait)
        if last_err:
            raise last_err
        return None

    final_text = ""
    # Repeat-call detector: (tool_name, args_json) → consecutive count
    last_call_sig: tuple[str, str] | None = None
    repeat_count = 0
    stop_nudge_sent = False
    keep_going_nudge_sent = False
    # Configurator/browser agents shouldn't declare victory after only landing
    # on a brand-root URL. Track the most-recent observed URL.
    last_observed_url: str = ""

    try:
        for step in range(max_iters):
            tracer.turn()
            resp = _generate_with_retry(contents)
            candidate = resp.candidates[0]
            parts = candidate.content.parts or []
            function_calls = [p.function_call for p in parts if getattr(p, "function_call", None)]

            if not function_calls:
                final_text = "".join(p.text for p in parts if getattr(p, "text", None)).strip()

                # Early-abort guard: for browser-navigation agents, reject a final answer
                # that ends on a brand-root URL after fewer than 5 real tool calls. Nudge
                # the agent to keep going instead of prematurely declaring victory.
                if (
                    agent_name == "configurator_recreator"
                    and not keep_going_nudge_sent
                    and len(tracer.trace.tool_events) < 5
                    and _is_brand_root(last_observed_url)
                ):
                    contents.append(candidate.content)
                    nudge = (
                        "WAIT — you've only opened the landing page and taken one screenshot. "
                        "You haven't actually navigated to the specific model yet. Do NOT write a final "
                        "report yet. Your next step: use click_by_text, click_link_by_href_contains, OR "
                        "navigate() with a direct model URL (see the URL patterns in your instructions) "
                        "to reach the specific car's page. Then take another screenshot. Keep going."
                    )
                    contents.append(types.Content(role="user", parts=[types.Part(text=nudge)]))
                    keep_going_nudge_sent = True
                    if verbose:
                        print(cli.c("  [early-abort detected — nudging agent to keep navigating]", "yellow"))
                    _emit("keep_going_nudge", {"reason": "brand_root_after_2_calls"})
                    continue

                # If the model produced a thin / apologetic response despite having real
                # trace data, attach a factual trace summary so the user always gets the
                # concrete outcome (URLs reached, screenshots saved, last failures).
                if _is_thin_response(final_text) and tracer.trace.tool_events:
                    factual = _synthesize_fallback_report(
                        tracer.trace.tool_events, user_input, reason="thin_response"
                    )
                    final_text = (final_text + "\n\n---\n\n" + factual).strip() if final_text else factual
                if verbose:
                    print(cli.dim(f"\n[agent finished in {step + 1} turn(s)]\n"))
                tracer.final(final_text)
                _emit("final", {"text": final_text, "turns": step + 1})
                break

            # --- Loop detection --------------------------------------------------
            # If the agent calls the same tool with the same args 3 times in a row,
            # inject a stop-nudge telling it to write a final report.
            this_sig = (function_calls[0].name, json.dumps(dict(function_calls[0].args or {}), sort_keys=True, default=str))
            if this_sig == last_call_sig:
                repeat_count += 1
            else:
                repeat_count = 1
                last_call_sig = this_sig
            if repeat_count >= 3 and not stop_nudge_sent:
                contents.append(candidate.content)
                nudge = (
                    "STOP. You've called the same tool with identical args 3 times and it keeps failing. "
                    "Do NOT apologize — this is expected when sites have complex UIs. Partial progress is success.\n\n"
                    "Write your final report as PLAIN TEXT (no more tool calls) in exactly this format:\n\n"
                    "Target config: <what the user asked for>\n"
                    "Reached: <last URL + page title you got to>\n"
                    "New-car price surfaced: <price if you saw one, else 'not found on this page'>\n"
                    "Screenshots captured: <list the labels: landing, model_selected, etc.>\n"
                    "What I could not do: <one line — e.g. 'could not select AMG Line trim; the picker did not respond to clicks'>\n"
                    "Match quality: <exact | close | model-family only | partial | failed>\n\n"
                    "Respond with that plain-text report NOW."
                )
                contents.append(types.Content(role="user", parts=[types.Part(text=nudge)]))
                stop_nudge_sent = True
                if verbose:
                    print(cli.c("  [loop detected — nudging agent to wrap up]", "yellow"))
                _emit("loop_nudge", {"repeated_tool": this_sig[0]})
                continue

            contents.append(candidate.content)

            tool_response_parts = []
            for fc in function_calls:
                name, args = fc.name, dict(fc.args or {})
                if verbose:
                    print(cli.tool_call(name, json.dumps(args, default=str)[:180]))
                _emit("tool_call", {"name": name, "args": _trunc(args)})

                impl = tool_impls.get(name)
                t0 = time.time()
                if impl is None:
                    result: dict = {"error": f"unknown tool {name}"}
                else:
                    try:
                        out = impl(**args)
                        result = out if isinstance(out, dict) else {"value": out}
                    except Exception as e:
                        result = {"error": f"{type(e).__name__}: {e}"}
                duration_ms = int((time.time() - t0) * 1000)

                tracer.tool_call(name, args, result, duration_ms=duration_ms)
                # Track most-recent URL observed (for early-abort detection)
                if isinstance(result, dict):
                    observed = result.get("url") or result.get("url_after")
                    if isinstance(observed, str) and observed.startswith("http"):
                        last_observed_url = observed
                if verbose:
                    print(cli.tool_result(json.dumps(result, default=str)[:200]))
                _emit("tool_result", {"name": name, "result": _trunc(result), "duration_ms": duration_ms})

                tool_response_parts.append(
                    types.Part.from_function_response(name=name, response=result)
                )

            contents.append(types.Content(role="user", parts=tool_response_parts))
        else:
            # Max iterations hit. Synthesize a useful fallback report from the trace
            # instead of a useless "agent hit max iterations" error.
            final_text = _synthesize_fallback_report(
                tracer.trace.tool_events, user_input, reason="max_iters"
            )
            tracer.final(final_text)
            tracer.error("max_iters_exceeded")
            _emit("final", {"text": final_text, "turns": max_iters, "error": "max_iters"})
            if verbose:
                print(cli.c("\n[max iterations reached — synthesized fallback report]\n", "yellow"))
    except Exception as e:
        final_text = f"[agent error: {type(e).__name__}: {e}]"
        tracer.error(f"{type(e).__name__}: {e}")
        _emit("error", {"error": str(e)})
        if verbose:
            print(cli.c(final_text, "red"))
    finally:
        trace_path = tracer.save()

    return final_text, trace_path


_APOLOGY_MARKERS = [
    "cannot fulfill", "unable to", "i'm sorry", "i am sorry", "i apologize",
    "i was unable", "i could not", "i couldn't", "i wasn't able",
]


_BRAND_ROOTS = [
    "bmw.de/de/neufahrzeuge.html",
    "bmw.com/en/all-models.html",
    "bmwusa.com",
    "mercedes-benz.de/passengercars/models.html",
    "mercedes-benz.it/passengercars/models.html",
    "mercedes-benz.com/en/vehicles",
    "mbusa.com",
    "audi.de/de/brand/de/neuwagen.html",
    "audi.com/en/models.html",
    "porsche.com/germany/models/",
    "porsche.com/germany/",
    "porsche.com/international/models/",
    "porsche.com/usa/models/",
    "volkswagen.de/de/modelle.html",
    "vw.com",
    "mini.de/de_de/home/range.html",
    "miniusa.com",
]


def _is_brand_root(url: str) -> bool:
    """True if the URL is a brand landing/all-models page, i.e. the agent hasn't
    actually navigated into a specific car's page yet."""
    if not url:
        return True
    low = url.lower().rstrip("/")
    if not low:
        return True
    for root in _BRAND_ROOTS:
        if low.endswith(root.rstrip("/")):
            return True
    return False


def _is_thin_response(text: str) -> bool:
    """True if the model's final answer is too short or pure apology — cases where
    we should augment it with a factual trace summary.
    """
    if not text:
        return True
    low = text.strip().lower()
    if len(low) < 140:
        return True
    # Apology present AND no URL → likely nothing actionable
    if any(m in low for m in _APOLOGY_MARKERS) and "http" not in low:
        return True
    return False


def _synthesize_fallback_report(tool_events: list, user_input: str, reason: str = "max_iters") -> str:
    """Build a best-effort summary from the agent's tool-call trace.

    `reason` controls the title:
      - "max_iters" → agent ran out of turns (couldn't produce a final text)
      - "thin_response" → agent gave a short/apologetic reply, we augment with facts
      - "partial_success" → agent finished but didn't reach the deepest goal
    """
    screenshots: list[str] = []
    urls_reached: list[str] = []
    last_errors: list[str] = []
    tool_counts: dict[str, int] = {}

    for ev in tool_events:
        name = getattr(ev, "name", None) or (ev.get("name") if isinstance(ev, dict) else "")
        result = getattr(ev, "result", None) or (ev.get("result") if isinstance(ev, dict) else {})
        tool_counts[name] = tool_counts.get(name, 0) + 1

        if isinstance(result, dict):
            if name == "take_screenshot" and result.get("path"):
                screenshots.append(result["path"])
            url = result.get("url") or result.get("url_after")
            if url and isinstance(url, str) and url not in urls_reached:
                urls_reached.append(url)
            if result.get("ok") is False:
                err = result.get("error", "")
                if err and err not in last_errors:
                    last_errors.append(err[:120])

    last_url = urls_reached[-1] if urls_reached else None
    recent_errors = last_errors[-3:] if last_errors else []

    title_map = {
        "max_iters":        "AGENT HIT MAX ITERATIONS — synthesized summary",
        "thin_response":    "TRACE SUMMARY (agent reply was brief — appending facts from tools)",
        "partial_success":  "TRACE SUMMARY — partial progress",
    }
    title = title_map.get(reason, "TRACE SUMMARY")

    has_screenshots = bool(screenshots)
    had_failures = bool(recent_errors)

    # Heuristic: if the last URL is either
    #   (a) on a brand's deep-configurator subdomain (configure.bmw.de,
    #       configurator.porsche.com, etc.), OR
    #   (b) has a model-specific path component (e.g. /911/, /c-class/, /golf/),
    # treat it as a CLOSE match even if some intermediate clicks failed.
    model_url_hit = False
    if last_url:
        url_low = last_url.lower()
        # (a) deep-configurator subdomains — the agent successfully entered the config SPA
        config_subdomains = [
            "configure.bmw.de",
            "configurator.porsche.com",
            "configurator.mercedes-benz",
            "konfigurator.",            # Audi, VW, Mini all use konfigurator.* subdomains
            "/configurator/",
            "/konfigurator/",
            "car-configurator.html",    # Mercedes's deep-config path
            "/configurator.html",       # Some brands (VW) use this
            "/konfigurator.html",
        ]
        if any(sd in url_low for sd in config_subdomains):
            model_url_hit = True
        else:
            model_tokens = [
                "carrera", "cayenne", "taycan", "macan", "911", "718",
                "panamera", "golf", "tiguan", "passat", "touareg", "arteon",
                "id.", "id3", "id4", "id5", "id7",
                "c-class", "c-klasse", "e-class", "e-klasse", "s-class",
                "glc", "gle", "gls", "cla", "cle",
                "3er", "4er", "5er", "7er", "x1", "x3", "x5", "x7",
                "m3", "m4", "m5", "m340", "i4", "ix",
                "a3", "a4", "a6", "a7", "a8", "q3", "q5", "q7", "q8",
                "rs3", "rs4", "rs6", "rs7", "e-tron",
                "cooper", "countryman", "clubman",
                "mini-3-tuerer", "mini-5-tuerer", "mini-cabrio", "/mini-",
            ]
            has_token = any(t in url_low for t in model_tokens)

            # Path-depth check (for sites that use normal URL paths)
            path = last_url.split("://", 1)[-1].split("/", 1)[-1].split("#", 1)[0] if "/" in last_url else ""
            depth = path.count("/")
            deep_path = depth >= 3

            # Hash-fragment check (for SPAs like porsche.com/germany/models/#modelRangeId=911)
            hash_with_token = "#" in last_url and any(t in last_url.lower().split("#", 1)[-1] for t in model_tokens)

            # Query-string check (for filtered landing pages like bmw.de/...?series=3)
            query_with_token = "?" in last_url and any(t in last_url.lower().split("?", 1)[-1] for t in model_tokens)

            if has_token and (deep_path or hash_with_token or query_with_token):
                model_url_hit = True

    if model_url_hit and has_screenshots:
        match = "CLOSE — reached the target model page (price likely visible in screenshot)"
    elif has_screenshots and not had_failures:
        match = "PARTIAL — reached pages but did not surface a final price"
    elif had_failures and not has_screenshots:
        match = "FAILED — navigation blocked; no screenshots captured"
    elif had_failures:
        match = "INCOMPLETE — reached pages but some navigation steps failed"
    else:
        match = "UNKNOWN — review trace"

    lines = [
        title,
        "",
        f"Target: {user_input[:160]}",
        f"Tool calls made: {sum(tool_counts.values())}  ·  Breakdown: {tool_counts}",
    ]
    if last_url:
        lines.append(f"Last page reached: {last_url}")
    if screenshots:
        lines.append(f"Screenshots captured ({len(screenshots)}):")
        for p in screenshots[-5:]:
            lines.append(f"  - {p}")
    if recent_errors:
        lines.append("Recent failures:")
        for e in recent_errors:
            lines.append(f"  - {e}")
    lines.append("")
    lines.append(f"Match quality: {match}")
    return "\n".join(lines)


def _trunc(obj: Any, max_chars: int = 600) -> Any:
    try:
        s = json.dumps(obj, default=str)
        if len(s) <= max_chars:
            return obj
        return {"_truncated": True, "preview": s[:max_chars]}
    except Exception:
        return {"_unserializable": str(type(obj))}
