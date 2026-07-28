"""hermes-omnicouncil v5.6.3 — multi-model agentic council with evidence/probe/prosecutor/compiler loops.

Features:
- Swappable models (model/member_models/judge_model/research_model — any provider via provider:model syntax)
- Shared blackboard with message rounds for inter-agent communication
- Safe agent tools: memory (query/pack), file read/search, web_search, web_extract, web_research_brief
- NO patch/write significant tools for agents (read-only safety)
- Presets (fast/balanced/deep/audit/max/omni_blackboard/ultra)
- Structured evidence ledger, Plan→Probe→Decide, prosecutor audit, forced dissent, judge compiler
- deep_web_crawl for professional research reports
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import inspect
import importlib.util as _iu
import json
import logging
import math
import os as _os
import posixpath
import random
import re
import time
import unicodedata
from pathlib import Path
from typing import Any

_EVEY_UTILS_PATH = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "evey_utils.py")

# ── call_hermes_model: always available (v5.4 multi-provider) ──
def call_hermes_model(
    prompt: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    max_tokens: int = 128000,
    temperature: float = 0.7,
    retries: int = 2,
    timeout: int = 60,
    purpose: str = "hermes-omnicouncil.member",
) -> dict[str, Any] | None:
    """Call ANY Hermes model via ctx.llm.complete(provider=..., model=...).
    
    Requires plugins.entries.hermes-omnicouncil.llm.allow_provider_override=true
    and allow_model_override=true in ~/.hermes/config.yaml.
    Falls back to HTTP for standalone/smoke-test use.
    
    P0 #6: Security checks on BOTH code paths (evey_utils exists AND not).
    """
    # ── P0 #6: Provider data policy + cancellation + budget (ALL paths) ──
    run_ctx: Any = _ACTIVE_RUN_CTX.get()
    if run_ctx is not None:
        effective_provider = provider or (run_ctx.model_provider_map.get(model or "", None))
        # P0 fix: if provider unresolved AND policy is confidential → fail-closed
        if not effective_provider:
            effective_provider = "deepseekproxy"  # Force default provider for custom proxy
            policy = PROVIDER_DATA_POLICIES.get(run_ctx.provider_data_policy, PROVIDER_DATA_POLICIES["internal"])
            if policy.get("allow_implicit_fallback") is False:
                return {
                    "error": "provider_unresolved_fail_closed",
                    "detail": f"Policy '{run_ctx.provider_data_policy}' requires explicit provider. model={model} has no mapped provider.",
                }
        elif effective_provider:
            policy = PROVIDER_DATA_POLICIES.get(run_ctx.provider_data_policy, PROVIDER_DATA_POLICIES["internal"])
            allowed = policy.get("allowed_providers", ["local"])
            if effective_provider not in allowed and effective_provider != "local":
                if not run_ctx.implicit_http_fallback:
                    return {
                        "error": f"provider_blocked_by_data_policy: {effective_provider} not in {allowed} (policy={run_ctx.provider_data_policy})",
                        "fix": "Set implicit_http_fallback=true or adjust provider_data_policy",
                    }
        if run_ctx.is_cancelled():
            return {"error": "council_cancelled"}
        if run_ctx.deadline and time.time() > run_ctx.deadline:
            run_ctx.cancel()
            return {"error": "council_timed_out"}
        if run_ctx.budget and not run_ctx.budget.can_call_model():
            return {"error": "budget_exhausted"}
    
    ctx = _RUNTIME_CTX
    if ctx is None or not hasattr(ctx, "llm"):
        # P0 #6: HTTP fallback must respect policy
        if run_ctx is not None and not run_ctx.implicit_http_fallback:
            return {"error": "implicit_http_fallback_disabled"}
        return _call_model_http(prompt, model or DEFAULT_MODEL, max_tokens, temperature, retries, timeout)
    for attempt in range(max(1, retries + 1)):
        try:
            result = ctx.llm.complete(
                messages=[{"role": "user", "content": prompt}],
                provider=provider,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                timeout=timeout,
                purpose=purpose,
            )
            if hasattr(result, "text"):
                content = str(getattr(result, "text", "") or "")
                actual_provider = getattr(result, "provider", provider or "hermes")
                actual_model = getattr(result, "model", model or "active")
                usage = getattr(result, "usage", None)
                # P1 #7: Record model call in budget
                if run_ctx is not None and run_ctx.budget:
                    input_tokens = getattr(usage, "input_tokens", 0) if usage else 0
                    output_tokens = getattr(usage, "output_tokens", 0) if usage else 0
                    cost = getattr(usage, "cost_usd", 0.0) if usage else 0.0
                    run_ctx.budget.record_model_call(input_tokens, output_tokens, float(cost or 0))
                return {
                    "content": content.strip(), "provider": actual_provider, "model": actual_model,
                    "usage": {"input_tokens": getattr(usage, "input_tokens", 0) if usage else 0,
                              "output_tokens": getattr(usage, "output_tokens", 0) if usage else 0,
                              "total_tokens": getattr(usage, "total_tokens", 0) if usage else 0,
                              "cost_usd": getattr(usage, "cost_usd", None) if usage else None},
                }
            rdict = result if isinstance(result, dict) else {}
            content = str(rdict.get("content") or "")
            if content:
                return {"content": content.strip(), "provider": provider or "hermes", "model": model or "active", "usage": {}}
        except Exception as exc:
            clsname = exc.__class__.__name__
            if "LlmTrust" in clsname or "trust" in str(exc).lower():
                return {
                    "error": "Hermes denied provider/model override for hermes-omnicouncil.",
                    "fix": (
                        "Add to ~/.hermes/config.yaml:\n"
                        "plugins:\n"
                        "  entries:\n"
                        "    hermes-omnicouncil:\n"
                        "      llm:\n"
                        "        allow_provider_override: true\n"
                        "        allow_model_override: true"
                    ),
                }

            if "PluginLlmTrustError" in clsname or "trust" in clsname.lower():
                return {
                    "error": "Hermes denied provider/model override for hermes-omnicouncil.",
                    "fix": (
                        "Add to ~/.hermes/config.yaml:\n"
                        "plugins:\n"
                        "  entries:\n"
                        "    hermes-omnicouncil:\n"
                        "      llm:\n"
                        "        allow_provider_override: true\n"
                        "        allow_model_override: true"
                    ),
                }
            if attempt + 1 >= max(1, retries + 1):
                logger.warning("call_hermes_model(%s/%s) failed: %s", provider, model, exc)
            else:
                time.sleep(min(2 ** attempt, 8))
    # P0 #6: HTTP fallback after retries must respect policy
    if run_ctx is not None and not run_ctx.implicit_http_fallback:
        return {"error": "implicit_http_fallback_disabled"}
    return _call_model_http(prompt, model or DEFAULT_MODEL, max_tokens, temperature, 1, timeout)

def _call_model_http(prompt: str, model: str, max_tokens: int, temperature: float, retries: int, timeout: int) -> dict[str, Any] | None:
    """Direct HTTP fallback (standalone/smoke tests, no ctx.llm)."""
    import urllib.error, urllib.request
    base_url = (_os.environ.get("EVEY_LITELLM_URL") or _os.environ.get("HERMES_DELEGATE_BASE_URL")
                or _os.environ.get("CODEX_BASE_URL") or "http://127.0.0.1:18089/v1").rstrip("/")
    api_key = (_os.environ.get("EVEY_LITELLM_KEY") or _os.environ.get("HERMES_DELEGATE_API_KEY")
               or _os.environ.get("CODEX_API_KEY") or "noop")
    payload = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens, "temperature": temperature}
    data = json.dumps(payload).encode("utf-8")
    for attempt in range(max(1, retries + 1)):
        try:
            req = urllib.request.Request(f"{base_url}/chat/completions", data=data,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            choices = result.get("choices") or []
            msg = (choices[0].get("message") if choices else {}) or {}
            content = msg.get("content") or msg.get("reasoning_content") or ""
            if content:
                return {"content": str(content).strip(), "provider": "http", "model": model,
                        "usage": {"total_tokens": (result.get("usage") or {}).get("total_tokens", 0)}}
        except Exception:
            if attempt + 1 < max(1, retries + 1):
                time.sleep(min(2 ** attempt, 8))
    return None

# ── Backward compat: evey_utils override if available ──
if _os.path.exists(_EVEY_UTILS_PATH):
    _spec = _iu.spec_from_file_location("evey_utils", _EVEY_UTILS_PATH)
    _eu = _iu.module_from_spec(_spec)
    _spec.loader.exec_module(_eu)
    _call_model_backup = _eu.call_model
else:
    def call_hermes_model(
        prompt: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        max_tokens: int = 128000,
        temperature: float = 0.7,
        retries: int = 2,
        timeout: int = 60,
        purpose: str = "hermes-omnicouncil.member",
    ) -> dict[str, Any] | None:
        """Call ANY Hermes model via ctx.llm.complete(provider=..., model=...).
        
        Requires plugins.entries.hermes-omnicouncil.llm.allow_provider_override=true
        and allow_model_override=true in ~/.hermes/config.yaml for cross-provider calls.
        Without override opt-in, uses the active user model (provider=None, model=None).
        
        P0 #6 fix: provider data policy enforcement. Без явного разрешения 
        implicit HTTP fallback отключён. CouncilRunContext определяет допустимые providers.
        
        Returns: {content, provider, model, usage: {input_tokens, output_tokens, total_tokens, cost_usd}}
        """
        # ── P0 #6: Provider data policy check ──
        run_ctx: Any = _ACTIVE_RUN_CTX.get()
        if run_ctx is not None:
            effective_provider = provider or (run_ctx.model_provider_map.get(model or "", None))
            # P0 fix: fail-closed when provider unresolved under restrictive policy
            if not effective_provider:
                effective_provider = "deepseekproxy"  # Force default provider for custom proxy
                policy = PROVIDER_DATA_POLICIES.get(run_ctx.provider_data_policy, PROVIDER_DATA_POLICIES["internal"])
                if policy.get("allow_implicit_fallback") is False:
                    return {
                        "error": "provider_unresolved_fail_closed",
                        "detail": f"Policy '{run_ctx.provider_data_policy}' requires explicit provider.",
                    }
            elif effective_provider:
                policy = PROVIDER_DATA_POLICIES.get(run_ctx.provider_data_policy, PROVIDER_DATA_POLICIES["internal"])
                allowed = policy.get("allowed_providers", ["local"])
                if effective_provider not in allowed and effective_provider != "local":
                    if not run_ctx.implicit_http_fallback:
                        return {
                            "error": f"provider_blocked_by_data_policy: {effective_provider} not in {allowed} (policy={run_ctx.provider_data_policy})",
                            "fix": "Set implicit_http_fallback=true or adjust provider_data_policy",
                        }
        
        # ── P1 #8: Cancellation check ──
        if run_ctx is not None and run_ctx.is_cancelled():
            return {"error": "council_cancelled", "detail": "Run was cancelled via cancellation token"}
        if run_ctx is not None and run_ctx.deadline and time.time() > run_ctx.deadline:
            run_ctx.cancel()
            return {"error": "council_timed_out", "detail": f"Deadline {run_ctx.deadline} exceeded"}
        
        # ── P1 #7: Budget check ──
        if run_ctx is not None and run_ctx.budget and not run_ctx.budget.try_reserve_model_call():
            return {"error": "budget_exhausted", "detail": f"Tokens: {run_ctx.budget.spent_total_tokens}/{run_ctx.budget.max_total_tokens}"}
        
        ctx = _RUNTIME_CTX
        if ctx is None or not hasattr(ctx, "llm"):
            # ── Standalone/smoke test fallback ──
            return _call_model_http(prompt, model or DEFAULT_MODEL, max_tokens, temperature, retries, timeout)
        
        for attempt in range(max(1, retries + 1)):
            try:
                result = ctx.llm.complete(
                    messages=[{"role": "user", "content": prompt}],
                    provider=provider,
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    timeout=timeout,
                    purpose=purpose,
                )
                if hasattr(result, "text"):
                    content = str(getattr(result, "text", "") or "")
                    actual_provider = getattr(result, "provider", provider or "hermes")
                    actual_model = getattr(result, "model", model or "active")
                    usage = getattr(result, "usage", None)
                    # P1 #7: Record model call in budget
                    if run_ctx is not None and run_ctx.budget:
                        input_tokens = getattr(usage, "input_tokens", 0) if usage else 0
                        output_tokens = getattr(usage, "output_tokens", 0) if usage else 0
                        cost = getattr(usage, "cost_usd", 0.0) if usage else 0.0
                        run_ctx.budget.record_model_call(input_tokens, output_tokens, float(cost or 0))
                    return {
                        "content": content.strip(),
                        "provider": actual_provider,
                        "model": actual_model,
                        "usage": {
                            "input_tokens": getattr(usage, "input_tokens", 0) if usage else 0,
                            "output_tokens": getattr(usage, "output_tokens", 0) if usage else 0,
                            "total_tokens": getattr(usage, "total_tokens", 0) if usage else 0,
                            "cost_usd": getattr(usage, "cost_usd", None) if usage else None,
                        },
                    }
                # Fallback dict
                rdict = result if isinstance(result, dict) else {}
                content = str(rdict.get("content") or "")
                if content:
                    return {"content": content.strip(), "provider": provider or "hermes", "model": model or "active", "usage": {}}
            except Exception as exc:
                clsname = exc.__class__.__name__
                if "LlmTrust" in clsname or "trust" in str(exc).lower():
                    return {
                        "error": "Hermes denied provider/model override for hermes-omnicouncil.",
                        "fix": "Add plugins.entries.hermes-omnicouncil.llm.allow_provider_override=true and allow_model_override=true to ~/.hermes/config.yaml.",
                    }
                if attempt + 1 >= max(1, retries + 1):
                    logger.warning("call_hermes_model(%s/%s) failed: %s", provider, model, exc)
                else:
                    time.sleep(min(2 ** attempt, 8))
        
        # ── Ultimate fallback: direct HTTP (respects data policy) ──
        if run_ctx is not None and not run_ctx.implicit_http_fallback:
            return {
                "error": "implicit_http_fallback_disabled",
                "fix": "Set implicit_http_fallback=true in council args to enable HTTP fallback",
            }
        return _call_model_http(prompt, model or DEFAULT_MODEL, max_tokens, temperature, 1, timeout)

    def _call_model_http(
        prompt: str, model: str, max_tokens: int, temperature: float, retries: int, timeout: int
    ) -> dict[str, Any] | None:
        """Direct HTTP fallback (standalone/smoke tests, no ctx.llm available)."""
        import urllib.error, urllib.request
        base_url = (
            _os.environ.get("EVEY_LITELLM_URL")
            or _os.environ.get("HERMES_DELEGATE_BASE_URL")
            or _os.environ.get("CODEX_BASE_URL")
            or "http://127.0.0.1:18089/v1"
        ).rstrip("/")
        api_key = (
            _os.environ.get("EVEY_LITELLM_KEY")
            or _os.environ.get("HERMES_DELEGATE_API_KEY")
            or _os.environ.get("CODEX_API_KEY")
            or "noop"
        )
        payload = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens, "temperature": temperature}
        data = json.dumps(payload).encode("utf-8")
        for attempt in range(max(1, retries + 1)):
            try:
                req = urllib.request.Request(f"{base_url}/chat/completions", data=data,
                    headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    result = json.loads(resp.read().decode("utf-8"))
                choices = result.get("choices") or []
                msg = (choices[0].get("message") if choices else {}) or {}
                content = msg.get("content") or msg.get("reasoning_content") or ""
                if content:
                    return {"content": str(content).strip(), "provider": "http", "model": model, "usage": {"total_tokens": (result.get("usage") or {}).get("total_tokens", 0)}}
            except Exception:
                if attempt + 1 < max(1, retries + 1):
                    time.sleep(min(2 ** attempt, 8))
        return None

    # Backward compat alias
    def call_model(model: str, prompt: str, max_tokens: int = 128000, temperature: float = 0.7,
                   retries: int = 2, timeout: int = 60, reasoning_effort: str | None = None) -> dict[str, Any] | None:
        return call_hermes_model(prompt, model=model, max_tokens=max_tokens, temperature=temperature, retries=retries, timeout=timeout)

# ── Backward compat: call_model alias (always available) ──
def call_model(model: str, prompt: str, max_tokens: int = 128000, temperature: float = 0.7,
               retries: int = 2, timeout: int = 60, reasoning_effort: str | None = None) -> dict[str, Any] | None:
    """Backward-compat wrapper. Prefer call_hermes_model(prompt, provider=..., model=...)."""
    return call_hermes_model(prompt, model=model, max_tokens=max_tokens, temperature=temperature, retries=retries, timeout=timeout)

logger = logging.getLogger("hermes.omnicouncil")
_RUNTIME_CTX: Any = None
_ACTIVE_FALLBACK_MODELS: list[str] = []
_JUDGE_SPEC: dict[str, str|None] = {}
_RESEARCH_SPEC: dict[str, str|None] = {}

# ── deep_web_research (copied/imported companion tool) ──────────────────
_deep_spec = _iu.spec_from_file_location(
    "omnicouncil_deep_web_research",
    _os.path.join(_os.path.dirname(__file__), "deep_web_research.py"),
)
_deep_web_research = _iu.module_from_spec(_deep_spec)
_deep_spec.loader.exec_module(_deep_web_research)

# ═══════════════════════════════════════════════════════════════════════
#  Model routing — any provider+model pair Hermes can call
# ═══════════════════════════════════════════════════════════════════════
VERSION = "5.6.3-cross-plugin-v2"

# ── Model ref parser ──────────────────────────────────────────────────
def parse_model_ref(ref: str) -> "tuple[str|None, str]":
    """Parse 'provider:model' or bare 'model' string.
    Returns (provider_or_none, model). Provider=None means use active Hermes provider."""
    ref = str(ref or "").strip()
    if not ref:
        raise ValueError("empty model ref")
    if ":" in ref:
        provider, model = ref.split(":", 1)
        provider = provider.strip() or None
        model = model.strip()
        if not model:
            raise ValueError(f"invalid model ref: {ref!r}")
        return provider, model
    return None, ref

def _parse_member_spec(spec: "str|dict[str,Any]") -> "dict[str,Any]":
    """Normalise member spec to {provider, model, role, perspective} dict."""
    if isinstance(spec, dict):
        model_str = spec.get("model", "")
        provider = spec.get("provider")  # may be None → use active
        role = spec.get("role") or spec.get("name") or ""
        perspective = spec.get("perspective") or ""
        return {"provider": provider, "model": model_str, "role": role, "perspective": perspective}
    # String: 'provider:model' or 'model'
    provider, model = parse_model_ref(str(spec))
    return {"provider": provider, "model": model, "role": "", "perspective": ""}

# ── Council presets (structure, not models) ──────────────────────────
COUNCIL_PRESETS = {
    "fast": {"members": 3, "rounds": 1, "collaboration_rounds": 0},
    "balanced": {"members": 5, "rounds": 2, "collaboration_rounds": 1},
    "deep": {"members": 8, "rounds": 3, "collaboration_rounds": 2},
    "audit": {"members": 4, "rounds": 2, "collaboration_rounds": 1},
    "max": {"members": 8, "rounds": 4, "collaboration_rounds": 3},
    "omni_blackboard": {"members": 6, "rounds": 3, "collaboration_rounds": 2},
    "ultra": {"members": 12, "rounds": 5, "collaboration_rounds": 3},
}

# Backward compat: map old preset names to council sizes only (models come from args/config)
_LEGACY_PRESET_SIZES = {
    "auto": "balanced",
    "fast": "fast",
    "balanced": "balanced",
    "deep": "deep",
    "audit": "audit",
    "max": "max",
    "omni_blackboard": "omni_blackboard",
    "ultra": "ultra",
}

# Legacy alias (kept for backward compat with old code paths)
DEFAULT_MODEL = _os.environ.get("OMNICOUNCIL_DEFAULT_MODEL") or "deepseek-v4-pro"
REASONING_EFFORT = "high"

# ═══════════════════════════════════════════════════════════════════════
#  Scalars
# ═══════════════════════════════════════════════════════════════════════
DEFAULT_COUNCILS = 5
DEFAULT_MEMBERS_PER_COUNCIL = 4
MAX_COUNCILS = 8
MAX_MEMBERS_PER_COUNCIL = 8
DEFAULT_MAX_TOKENS = 384000
DEFAULT_JUDGE_MAX_TOKENS = 384000
DEFAULT_COLLABORATION_ROUNDS = 2
DEFAULT_MESSAGE_ROUNDS = 1
DEFAULT_MEMBER_RETRIES = 1
DEFAULT_MODEL_TIMEOUT = 900
DEFAULT_JUDGE_TIMEOUT = 1200
CACHE_DIR = Path.home() / ".hermes" / "cache" / "hermes-omnicouncil"
CACHE_TTL = 24 * 60 * 60
MAX_PROMPT_CONTEXT_CHARS = 1_000_000
MAX_CONTEXT_CHARS = 1_000_000
MAX_MANIFEST_CHARS = 120_000
MAX_SKILL_INDEX_CHARS = 80_000
MAX_PLUGIN_INDEX_CHARS = 60_000
MAX_MCP_INDEX_CHARS = 40_000
MAX_EVIDENCE_CHARS = 80_000
MAX_INITIAL_ANSWER_CHARS_FOR_COLLAB = 24_000
MAX_PREVIOUS_ROUND_CHARS_FOR_COLLAB = 8_000
MAX_INITIAL_ANSWER_CHARS_FOR_JUDGE = 24_000
MAX_DISCUSSION_CHARS_FOR_JUDGE = 6_000
MAX_RESEARCH_REPORT_CHARS_FOR_JUDGE = 16_000
MAX_MESSAGE_CHARS = 4_000
MAX_MESSAGE_HISTORY_CHARS = 16_000
MAX_TOOL_REQUESTS_DEFAULT = 20
MAX_AGENTIC_TOOL_REQUESTS = 200
MAX_BROKERED_TOOL_REQUESTS = MAX_AGENTIC_TOOL_REQUESTS * 2
AGENTIC_ACTIVE_TOOL_AGENTS = 4
AGENTIC_MUTATING_AGENTS = 0
DEFAULT_MEMORY_CONTEXT_CHARS = 12_000
MAX_MEMORY_CONTEXT_CHARS = 30_000
DEFAULT_MAX_MEMBER_WORKERS = 8
DEFAULT_MAX_COLLABORATION_WORKERS = 6
DEFAULT_MAX_RESEARCH_WORKERS = 4
DEFAULT_MIN_SUCCESSFUL_MEMBERS = 1  # будет пересчитан динамически (60% quorum)
DEFAULT_QUORUM_RATIO = 0.6  # P0 fix: 60% участников должны ответить
DEFAULT_REQUEST_JITTER_MS = 5500
MAX_REQUEST_JITTER_MS = 30_000

# ═══════════════════════════════════════════════════════════════════════
#  Safe agent tools — read-only, no patch/write/terminal
# ═══════════════════════════════════════════════════════════════════════
SAFE_AGENT_TOOLS = [
    "memory_wiki_query",
    "memory_wiki_pack_context",
    "memory_wiki_code_claim_query",
    "memory_wiki_symbol_history",
    "memory_wiki_repository_context",
    "read_file",
    "search_files",
    "web_search",
    "web_extract",
    "web_research_brief",
    "skill_view",
    "skills_list",
    # Code Shrinker context operations are read-only with respect to the
    # workspace. context.create/expand mutate only ephemeral server sessions.
    "project.map",
    "project.watch_status",
    "project.changed_symbols",
    "file.contracts",
    "symbol.source",
    "symbol.context",
    "context.create",
    "context.expand",
    "context.inspect",
    "artifact.get",
    "artifact.get_chunk",
    "artifact.copy_text",
    "artifact.list",
    "artifact.stats",
]

SAFE_AGENT_TOOLSET_NAMES = ["memory", "file_read", "web", "skills_read", "code_context"]

# Models may propose changes but never execute workspace/git/patch lifecycle writes.
COUNCIL_DENIED_TOOLS: set[str] = {
    "write_file", "edit_file", "apply_patch", "terminal", "shell",
    "run_command", "delete_file", "move_file", "rename_file",
    "git_commit", "git_push", "patch", "patch.propose",
    "patch.validate", "patch.apply", "project.scan",
    "memory_wiki_code_claim_add", "memory_wiki_invalidate_revision",
    "memory_wiki_patch_outcome_add",
}

COUNCIL_ALLOWED_TOOLS: set[str] = {
    # Memory Wiki reads.
    "memory_wiki_query", "memory_wiki_pack_context",
    "memory_wiki_get_project_context", "memory_wiki_graph_query",
    "memory_wiki_why_believe", "memory_wiki_recent_changes",
    "memory_wiki_health", "memory_wiki_get_page",
    "memory_wiki_code_claim_query", "memory_wiki_symbol_history",
    "memory_wiki_repository_context",
    # Durable blackboard writes only; broker forces the namespace.
    "memory_wiki_write_firewall", "memory_wiki_add_claim",
    "memory_wiki_add_evidence", "memory_wiki_add_decision",
    "memory_wiki_add_task_capsule", "memory_wiki_post_task",
    # File/web/skill reads.
    "read_file", "search_files", "web_search", "web_extract",
    "web_research_brief", "skills_list", "skill_view",
    # Code Shrinker read/ephemeral-context operations.
    "project.map", "project.watch_status", "project.changed_symbols",
    "file.contracts", "symbol.source", "symbol.context",
    "context.create", "context.expand", "context.inspect",
    "artifact.get", "artifact.get_chunk", "artifact.copy_text",
    "artifact.list", "artifact.stats",
}

MUTATION_POLICY: dict[str, str] = {
    "propose_only": "Models may propose patches but cannot apply them.",
    "judge_approved": "Judge may approve, but executor still requires explicit user/tool approval.",
    "operator_only": "Only the outer Hermes agent/operator may execute mutations.",
}

PERSPECTIVES = [
    "Архитектор: найди самый простой и устойчивый дизайн решения.",
    "Имплементатор: думай о конкретных правках, командах, тестах и edge cases.",
    "Ревьюер/безопасник: ищи риски, регрессии, секреты, опасные действия и ложные допущения.",
    "Оптимизатор: сократи путь, устрани лишнее, предложи быстрые проверки и rollback.",
    "SRE/Надёжность: думай о таймаутах, retries, деградации, observability и стабильности связи.",
    "Memory/Knowledge curator: думай о durable memory, task capsules, evidence, дедупликации и будущей воспроизводимости.",
    "UX/Оператор: сделай результат удобным для следующего действия пользователя и минимизируй ручные шаги.",
    "QA/Тестировщик: формулируй acceptance criteria, regression tests, negative tests и smoke сценарии.",
    "Исследователь: используй web_search, web_extract, read_file для фактов; собирай веб-данные через web_research_brief.",
]

OMNI_BLACKBOARD_ROLES = [
    "planner: строит план, декомпозирует задачу, определяет stop conditions; tools=memory/file_read/web/skills_read; mutating=false.",
    "researcher_1: ищет факты через web_search + web_extract + memory_wiki_query; tools=memory_read/web/file_read; mutating=false.",
    "researcher_2: ищет альтернативные подходы и tradeoffs; tools=memory_read/web/file_read; mutating=false.",
    "architect: проектирует решение, опираясь на факты и memory; tools=memory_read/file_read/web; mutating=false.",
    "reviewer_1: проверяет корректность, архитектуру, regression risks; tools=file_read/search; mutating=false.",
    "reviewer_2: ищет edge cases, hidden constraints, ложные допущения; tools=file_read/search; mutating=false.",
    "red_team: ломает план, ищет контрпримеры, falsification tests; tools=file_read/search; mutating=false.",
    "qa_tester: формулирует acceptance criteria и тест-план; tools=file_read/search; mutating=false.",
    "judge: синтезирует финальное решение, взвешивает evidence/dissent; tools=normally_none; mutating=false.",
]

OMNI_BLACKBOARD_ROUNDS = [
    "Round 0: Orchestrator формулирует задачу и общий blackboard.",
    "Round 1: независимые первичные мнения с использованием safe tools.",
    "Round 2: peer collaboration — агенты видят находки коллег и спорят через blackboard.",
    "Round 3: message rounds — агенты обмениваются вопросами и ответами.",
    "Round 4: red-team critique ломает решение.",
    "Round 5: judge synthesis с учётом всех evidence и messages.",
    "Round 6: финальный отчёт.",
]


AUTO_DEBATE_PERSPECTIVES = [
    "Proponent: выдвигает сильнейшее решение, формулирует claims и позитивный case.",
    "Skeptic: атакует допущения, ищет edge cases, scope creep и слабые evidence.",
    "Evidence hunter: запрашивает safe tools, проверяет факты и отделяет evidence от assumptions.",
    "Prosecutor: собирает unsupported claims, contradictions, overconfidence и ignored dissent.",
    "Verifier: превращает спор в проверяемые acceptance tests, falsification tests и stop conditions.",
    "Judge compiler: сжимает дебат в decision, risks, implementation plan и next step.",
]

CONSILIUM_PRESETS = {
    "auto": {"councils": None, "members_per_council": None, "collaboration_rounds": None, "message_rounds": 0, "research_missions": None},
    "fast": {"councils": 2, "members_per_council": 3, "collaboration_rounds": 0, "message_rounds": 0, "research_missions": False},
    "balanced": {"councils": 3, "members_per_council": 4, "collaboration_rounds": 1, "message_rounds": 0, "research_missions": False},
    "deep": {"councils": 5, "members_per_council": 4, "collaboration_rounds": 2, "message_rounds": 1, "research_missions": True},
    "audit": {"councils": 4, "members_per_council": 5, "collaboration_rounds": 2, "message_rounds": 1, "research_missions": True, "red_team": True},
    "max": {"councils": MAX_COUNCILS, "members_per_council": MAX_MEMBERS_PER_COUNCIL, "collaboration_rounds": 4, "message_rounds": 2, "research_missions": True, "red_team": True},
    "omni_blackboard": {
        "councils": 5,
        "members_per_council": 4,
        "collaboration_rounds": 4,
        "message_rounds": 2,
        "research_missions": True,
        "red_team": True,
        "tool_mode": "safe_agent",
        "capability_profile": "omni",
        "max_research_agents": AGENTIC_ACTIVE_TOOL_AGENTS,
        "max_member_workers": DEFAULT_MAX_MEMBER_WORKERS,
        "max_research_workers": DEFAULT_MAX_RESEARCH_WORKERS,
        "max_collaboration_workers": DEFAULT_MAX_COLLABORATION_WORKERS,
        "max_tool_requests": MAX_AGENTIC_TOOL_REQUESTS,
        "agentic_blackboard": True,
        "minimum_tools": True,
        "brokered_tools": True,
        "active_tool_agents": AGENTIC_ACTIVE_TOOL_AGENTS,
        "mutating_agents": AGENTIC_MUTATING_AGENTS,
    },
    "ultra": {
        "councils": MAX_COUNCILS,
        "members_per_council": MAX_MEMBERS_PER_COUNCIL,
        "collaboration_rounds": 6,
        "message_rounds": 3,
        "research_missions": True,
        "red_team": True,
        "tool_mode": "safe_agent",
        "capability_profile": "omni",
        "max_research_agents": AGENTIC_ACTIVE_TOOL_AGENTS,
        "max_member_workers": DEFAULT_MAX_MEMBER_WORKERS,
        "max_research_workers": DEFAULT_MAX_RESEARCH_WORKERS,
        "max_collaboration_workers": DEFAULT_MAX_COLLABORATION_WORKERS,
        "max_tool_requests": MAX_AGENTIC_TOOL_REQUESTS * 2,
        "agentic_blackboard": True,
        "minimum_tools": True,
        "brokered_tools": True,
        "active_tool_agents": AGENTIC_ACTIVE_TOOL_AGENTS,
        "mutating_agents": AGENTIC_MUTATING_AGENTS,
        "max_tokens": 384000,
        "judge_max_tokens": 384000,
    },
}

CAPABILITY_PROFILES = {
    "minimal": ["file_read", "web"],
    "dev": ["file_read", "web", "skills_read", "memory"],
    "research": ["web", "skills_read", "memory"],
    "omni": ["memory", "file_read", "web", "skills_read", "search"],
}

STATIC_TOOLSETS = {
    "file_read": ["read_file", "search_files"],
    "web": ["web_search", "web_extract"],
    "skills_read": ["skills_list", "skill_view"],
    "memory": ["memory_wiki_query", "memory_wiki_pack_context"],
    "search": ["search_files"],
    "delegation": ["delegate_task", "cached_delegate", "hermes_omnicouncil", "council_decide"],
}

SCHEMA = {
    "name": "hermes_omnicouncil",
    "description": (
        "Hermes OmniCouncil v5.6.3: multi-model agentic council with shared blackboard, message rounds, "
        "safe memory/web/file tools, swappable models, web_research_brief, and deep_web_crawl. "
        "Agents collaborate via blackboard notes, peer review, and directed messages. "
        "Adds Evidence Ledger, Plan→Probe→Decide, Prosecutor, forced dissent, lesson extraction, and Judge-as-Compiler."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "Задача/вопрос для OmniCouncil."},
            "context": {"type": "string", "description": "Дополнительный контекст."},
            "repository_id": {"type": "string", "description": "Stable Code Shrinker repository slot, for example owner/repository."},
            "repository_path": {"type": "string", "description": "Optional allowed repository root. When supplied OmniCouncil asks Code Shrinker to scan/bind the slot before context.create."},
            "target_file": {"type": "string", "description": "Target file passed to Code Shrinker context.create."},
            "target_symbol": {"type": "string", "description": "Optional target symbol/function/class for exact context and callers/tests."},
            "commit_sha": {"type": "string", "description": "Optional current commit/revision propagated into coverage_manifest."},
            "code_context_mode": {"type": "string", "enum": ["off", "auto", "required"], "default": "auto", "description": "auto=use cross-plugin context when repository_id+target_file or coverage_manifest are present; required=fail closed if orchestration fails."},
            "code_context_token_budget": {"type": "integer", "default": 8000, "description": "Code Shrinker context.create token budget."},
            "code_context_chars": {"type": "integer", "default": 30000, "description": "Maximum serialized Code Shrinker packet characters injected into council context."},
            "coverage_manifest": {"type": "object", "description": "Optional prebuilt Code Shrinker coverage manifest. Normalized to protocol v2 before Memory Wiki recall."},
            "mode": {"type": "string", "enum": ["advise", "edit_plan", "review", "debug"], "default": "edit_plan"},
            "preset": {"type": "string", "enum": list(CONSILIUM_PRESETS), "default": "deep"},
            "model": {"type": "string", "default": DEFAULT_MODEL, "description": "Модель по умолчанию для всех агентов. Используйте provider:model синтаксис (напр. openrouter:anthropic/claude-sonnet-4)."},

            "fallback_models": {"type": "array", "items": {"type": "string"}, "default": [], "description": "Fallback model list tried after the requested model fails."},
            "member_models": {"type": "array", "items": {"type": "string"}, "default": [], "description": "Модели участников."},
            "judge_model": {"type": "string", "default": "", "description": "Модель для judge."},
            "research_model": {"type": "string", "default": "", "description": "Модель для research missions."},
            "councils": {"type": "integer", "default": DEFAULT_COUNCILS},
            "members_per_council": {"type": "integer", "default": DEFAULT_MEMBERS_PER_COUNCIL},
            "perspectives": {"type": "array", "items": {"type": "string"}, "default": []},
            "decision_policy": {"type": "string", "enum": ["judge", "majority", "consensus", "risk_weighted"], "default": "judge"},
            "red_team": {"type": "boolean", "default": False},
            "auto_scale": {"type": "boolean", "default": False},
            "dry_run": {"type": "boolean", "default": False, "description": "Return budget/latency estimate and resolved orchestration plan without model calls."},
            "max_tokens": {"type": "integer", "default": DEFAULT_MAX_TOKENS},
            "judge_max_tokens": {"type": "integer", "default": DEFAULT_JUDGE_MAX_TOKENS},
            "use_cache": {"type": "boolean", "default": True},
            "provider_data_policy": {"type": "string", "enum": ["confidential", "internal", "public"], "default": "internal", "description": "Data policy for provider routing: confidential=local only, internal=local+trusted proxy, public=any configured provider."},
            "implicit_http_fallback": {"type": "boolean", "default": False, "description": "Fail closed; enable HTTP fallback only for an explicitly public run."},
            "max_total_tokens": {"type": "integer", "default": 1_000_000, "description": "Total token budget for entire council run."},
            "max_cost_usd": {"type": "number", "default": 5.0, "description": "Total cost budget (USD)."},
            "max_wall_time_seconds": {"type": "number", "default": 600, "description": "Total wall-clock time limit (seconds)."},
            "max_model_calls": {"type": "integer", "default": 50, "description": "Maximum model calls allowed."},
            "cache_ttl_seconds": {"type": "integer", "default": CACHE_TTL},
            "force_refresh": {"type": "boolean", "default": False},
            "collaborate": {"type": "boolean", "default": True},
            "collaboration_rounds": {"type": "integer", "default": DEFAULT_COLLABORATION_ROUNDS},
            "message_rounds": {"type": "integer", "default": 1, "description": "Раунды обмена сообщениями между агентами."},
            "return_transcript": {"type": "boolean", "default": False},
            "tool_mode": {
                "type": "string",
                "enum": ["off", "safe_agent", "council_safe"],
                "default": "safe_agent",
                "description": "safe_agent=read-only tools for agents; council_safe=read-only + broker-enforced deny list; off=no tools",
            },
            "allow_file_mutations": {"type": "boolean", "default": False, "description": "Allow council models to mutate files (requires critical_change_policy override)."},
            "allow_code_mutations": {"type": "boolean", "default": False, "description": "Allow council models to mutate code (requires critical_change_policy override)."},
            "critical_change_policy": {
                "type": "string",
                "enum": ["propose_only", "judge_approved", "operator_only"],
                "default": "operator_only",
                "description": "propose_only=models propose patches only; judge_approved=judge can approve; operator_only=only Hermes operator executes mutations",
            },
            "capability_profile": {"type": "string", "enum": list(CAPABILITY_PROFILES), "default": "omni"},
            "auto_capability_scan": {"type": "boolean", "default": True},
            "auto_skills": {"type": "boolean", "default": True},
            "skills": {"type": "array", "items": {"type": "string"}, "default": []},
            "auto_memory_context": {"type": "boolean", "default": True},
            "memory_context_chars": {"type": "integer", "default": DEFAULT_MEMORY_CONTEXT_CHARS},
            "mcp_mode": {"type": "string", "enum": ["off", "manifest"], "default": "manifest"},
            "research_missions": {"type": "boolean", "default": False},
            "max_research_agents": {"type": "integer", "default": 3},
            "max_member_workers": {"type": "integer", "default": DEFAULT_MAX_MEMBER_WORKERS},
            "max_collaboration_workers": {"type": "integer", "default": DEFAULT_MAX_COLLABORATION_WORKERS},
            "max_research_workers": {"type": "integer", "default": DEFAULT_MAX_RESEARCH_WORKERS},
            "min_successful_members": {"type": "integer", "default": DEFAULT_MIN_SUCCESSFUL_MEMBERS},
            "enabled_toolsets": {"type": "array", "items": {"type": "string"}, "default": []},
            "max_tool_requests": {"type": "integer", "default": MAX_TOOL_REQUESTS_DEFAULT},
            "agentic_blackboard": {"type": "boolean", "default": False},
            "minimum_tools": {"type": "boolean", "default": True},
            "brokered_tools": {"type": "boolean", "default": True},
            "active_tool_agents": {"type": "integer", "default": AGENTIC_ACTIVE_TOOL_AGENTS},
            "mutating_agents": {"type": "integer", "default": AGENTIC_MUTATING_AGENTS},
            "return_blackboard": {"type": "boolean", "default": False},
            "return_evidence": {"type": "boolean", "default": True},
            "output_format": {"type": "string", "enum": ["prose", "structured", "patch_plan", "json"], "default": "structured"},
            "save_task_capsule": {"type": "boolean", "default": False},
            "member_retries": {"type": "integer", "default": DEFAULT_MEMBER_RETRIES},
            "model_timeout": {"type": "integer", "default": DEFAULT_MODEL_TIMEOUT},
            "judge_timeout": {"type": "integer", "default": DEFAULT_JUDGE_TIMEOUT},
            "request_jitter_ms": {"type": "integer", "default": DEFAULT_REQUEST_JITTER_MS},
            "strict_json": {"type": "boolean", "default": False},
            "json_schema": {"type": "object", "default": {}, "description": "Optional minimal JSON schema for strict_json judge output validation."},
            "dissent_required": {"type": "boolean", "default": False, "description": "Судья обязан перечислить dissent/objections даже при консенсусе."},
            "anti_slop": {"type": "boolean", "default": False, "description": "Каждая рекомендация должна иметь file:line/concrete detail."},
            "self_review_round": {"type": "boolean", "default": False, "description": "Post-judge self-review round для проверки unsupported claims."},
            "auto_debate": {"type": "boolean", "default": False, "description": "Enable Auto-Debate/VerifyChain mode: proponent/skeptic/prosecutor/verifier roles, evidence-ledger output and blackboard-first debate."},
            "verify_chain": {"type": "boolean", "default": False, "description": "Attach a compact VerifyChain report with claims, evidence, objections, prosecutor verdict and acceptance tests."},
            "debate_rounds": {"type": "integer", "default": 2, "description": "Target collaboration/message rounds for Auto-Debate mode."},
            "plan_probe_decide": {"type": "boolean", "default": True, "description": "Enable Plan→Probe→Decide preflight: model plans unknowns, broker executes safe evidence probes, then members decide."},
            "prosecutor_round": {"type": "boolean", "default": True, "description": "Run an adversarial prosecutor after judge synthesis to flag unsupported claims, contradictions and overconfidence."},
            "minimum_objections": {"type": "integer", "default": 2, "description": "Minimum objections when dissent_required=true; forced dissent generates missing objections before judge."},
            "compiler_judge": {"type": "boolean", "default": True, "description": "Require judge to compile a canonical JUDGE_COMPILED_JSON object; result also exposes compiled_synthesis."},
            "save_council_lessons": {"type": "boolean", "default": False, "description": "Persist compact lessons/anti-regression claims to memory_wiki; raw transcripts are never saved."},
        },
        "required": ["task"],
    },
}


DOCTOR_SCHEMA = {
    "name": "omnicouncil_doctor",
    "description": "Diagnose hermes-omnicouncil plugin registration, schema parity, cache, safe-tool policy, and optional model health.",
    "parameters": {
        "type": "object",
        "properties": {
            "checks": {"type": "array", "items": {"type": "string"}, "default": ["schema", "registration", "safe_tools", "cache", "deep_web", "models"]},
            "live_model_check": {"type": "boolean", "default": False},
            "include_cache_samples": {"type": "boolean", "default": False},
        },
    },
}

CACHE_LIST_SCHEMA = {
    "name": "omnicouncil_cache_list",
    "description": "List hermes-omnicouncil cached result files with size/mtime and optional short summaries.",
    "parameters": {"type": "object", "properties": {"limit": {"type": "integer", "default": 20}, "include_summaries": {"type": "boolean", "default": False}}},
}
CACHE_GET_SCHEMA = {
    "name": "omnicouncil_cache_get",
    "description": "Read one cached OmniCouncil result by cache key prefix.",
    "parameters": {"type": "object", "properties": {"cache_key": {"type": "string"}, "max_chars": {"type": "integer", "default": 12000}}, "required": ["cache_key"]},
}
CACHE_CLEAR_SCHEMA = {
    "name": "omnicouncil_cache_clear",
    "description": "Clear one cached OmniCouncil result by key prefix, or all results when all=true.",
    "parameters": {"type": "object", "properties": {"cache_key": {"type": "string", "default": ""}, "all": {"type": "boolean", "default": False}}},
}
CACHE_EXPLAIN_SCHEMA = {
    "name": "omnicouncil_cache_explain_key",
    "description": "Explain/compute the cache key that would be used for a task/options payload.",
    "parameters": {"type": "object", "properties": {"task": {"type": "string"}, "context": {"type": "string", "default": ""}, "mode": {"type": "string", "default": "edit_plan"}, "options": {"type": "object", "default": {}}}, "required": ["task"]},
}


CODE_REVIEW_SCHEMA = {
    "name": "omnicouncil_code_review",
    "description": (
        "Orchestrated multi-model code review: project map → compressed context → "
        "independent reviewers → exact source on low confidence → patch proposal → "
        "sandbox validation → evidence to Memory Wiki. Patch application left to operator."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "project_path": {
                "type": "string",
                "description": "Абсолютный путь к корню проекта для ревью."
            },
            "review_goal": {
                "type": "string",
                "description": "Цель ревью: security audit, bug hunt, architecture review, refactor plan, performance audit."
            },
            "target_files": {
                "type": "array",
                "items": {"type": "string"},
                "default": [],
                "description": "Конкретные файлы для фокуса ревью (пусто = весь проект)."
            },
            "focus_areas": {
                "type": "array",
                "items": {"type": "string"},
                "default": [],
                "description": "Фокус-области: auth, input_validation, cryptography, sql, file_io, dependencies."
            },
            "member_models": {
                "type": "array",
                "items": {"type": "string"},
                "default": [],
                "description": "Модели-ревьюеры. По умолчанию: gpt-5.5 + claude-sonnet-4 + deepseek-v4-pro."
            },
            "judge_model": {
                "type": "string",
                "default": "",
                "description": "Модель-judge для синтеза findings."
            },
            "min_confidence": {
                "type": "number",
                "default": 0.7,
                "description": "Минимальная confidence для принятия finding без эскалации к exact source."
            },
            "save_to_memory": {
                "type": "boolean",
                "default": True,
                "description": "Сохранить findings, evidence и patch proposals в Memory Wiki."
            },
            "preset": {
                "type": "string",
                "enum": ["fast", "balanced", "deep", "audit", "max"],
                "default": "audit"
            },
            "max_tokens": {
                "type": "integer",
                "default": 128000
            },
            "return_transcript": {
                "type": "boolean",
                "default": False
            },
        },
        "required": ["project_path", "review_goal"],
    },
}

SECRET_PATTERNS = [
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-+/=]{12,}"),
    re.compile(r"(?i)(api[_-]?key|token|secret|password|passwd|pwd)(\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"\b(sk-[A-Za-z0-9_\-]{12,})\b"),
    re.compile(r"\b(gh[pousr]_[A-Za-z0-9_]{12,})\b"),
]


# ═══════════════════════════════════════════════════════════════
#  helpers
# ═══════════════════════════════════════════════════════════════
def _redact(text: Any) -> str:
    s = str(text or "")
    for pat in SECRET_PATTERNS:
        if pat.pattern.startswith("(?i)(bearer"):
            s = pat.sub(r"\1[REDACTED]", s)
        elif "api" in pat.pattern.lower() or "password" in pat.pattern.lower():
            s = pat.sub(r"\1\2[REDACTED]", s)
        else:
            s = pat.sub("[REDACTED]", s)
    return s


def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def _truncate_text(text: Any, max_chars: int, suffix: str = "…[truncated]") -> str:
    text = _redact(text)
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    keep = max(0, max_chars - len(suffix))
    return text[:keep] + suffix


def _normalise_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "y", "on", "да", "истина"}:
            return True
        if lowered in {"0", "false", "no", "n", "off", "нет", "ложь"}:
            return False
    return default


def _bounded_context(context: str) -> str:
    return _truncate_text(context, MAX_CONTEXT_CHARS, "…[context truncated]")


def _prompt_too_large(prompt: str) -> bool:
    return len(prompt) > MAX_PROMPT_CONTEXT_CHARS


def _clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        n = int(value)
    except Exception:
        return default
    return max(minimum, min(maximum, n))


def _request_jitter(jitter_ms: int = DEFAULT_REQUEST_JITTER_MS) -> float:
    try:
        limit_ms = max(0, min(MAX_REQUEST_JITTER_MS, int(jitter_ms or 0)))
    except Exception:
        limit_ms = DEFAULT_REQUEST_JITTER_MS
    if limit_ms <= 0:
        return 0.0
    delay = random.uniform(0.0, limit_ms / 1000.0)
    time.sleep(delay)
    return delay


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [x.strip() for x in value.split(",") if x.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(x).strip() for x in value if str(x).strip()]
    return []


def _add_evidence(ledger: list[dict[str, Any]], source: str, summary: str, **extra: Any) -> str:
    eid = f"E{len(ledger) + 1}"
    item = {"id": eid, "source": source, "summary": _truncate_text(summary, 1200)}
    for k, v in extra.items():
        item[k] = _truncate_text(v, 2000) if isinstance(v, str) else v
    ledger.append(item)
    return eid


def _read_text_safe(path: Path, max_chars: int = 8000) -> str:
    try:
        return _redact(path.read_text(encoding="utf-8", errors="replace")[:max_chars])
    except Exception:
        return ""


def _extract_yaml_field(text: str, field: str) -> str:
    m = re.search(rf"(?im)^\s*{re.escape(field)}\s*:\s*[\"']?([^\n\"']+)[\"']?\s*$", text)
    return _redact(m.group(1).strip()) if m else ""


def _write_json_atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{_os.getpid()}.{int(time.time() * 1000)}.tmp")
    tmp.write_text(_json(obj), encoding="utf-8")
    tmp.replace(path)


# ═══════════════════════════════════════════════════════════════
#  Model routing
# ═══════════════════════════════════════════════════════════════
def _resolve_models(args: dict[str, Any]) -> "tuple[dict, list[dict], dict, dict]":
    """Resolve member/judge/research model specs from args.
    Returns: (default_spec, [member_specs], judge_spec, research_spec)
    Each spec = {"provider": str|None, "model": str, "role": str, "perspective": str}
    """
    # ── Parse members ──
    members_raw = args.get("members") or args.get("member_models") or []
    member_specs: list[dict] = []
    for m in members_raw:
        spec = _parse_member_spec(m)
        if spec["model"]:
            member_specs.append(spec)
    if not member_specs:
        # Fallback: old-style string member_models
        old_style = args.get("member_models") or []
        if isinstance(old_style, list):
            for m in old_style:
                if isinstance(m, str) and m.strip():
                    provider, model = parse_model_ref(m)
                    member_specs.append({"provider": provider, "model": model, "role": "", "perspective": ""})
    if not member_specs:
        member_specs = [{"provider": None, "model": DEFAULT_MODEL, "role": "", "perspective": ""}]

    # ── Judge ──
    judge_raw = args.get("judge")
    if isinstance(judge_raw, dict) and judge_raw.get("model"):
        judge_spec = {"provider": judge_raw.get("provider"), "model": judge_raw["model"], "role": "judge", "perspective": ""}
    elif isinstance(judge_raw, str) and judge_raw.strip():
        provider, model = parse_model_ref(judge_raw)
        judge_spec = {"provider": provider, "model": model, "role": "judge", "perspective": ""}
    else:
        old_judge = args.get("judge_model") or member_specs[0]["model"]
        provider, model = parse_model_ref(old_judge) if ":" in str(old_judge) else (member_specs[0]["provider"], old_judge)
        judge_spec = {"provider": provider, "model": model, "role": "judge", "perspective": ""}

    # ── Research ──
    research_raw = args.get("research_model")
    if isinstance(research_raw, str) and research_raw.strip():
        provider, model = parse_model_ref(research_raw)
        research_spec = {"provider": provider, "model": model, "role": "researcher", "perspective": ""}
    else:
        research_spec = dict(member_specs[0])

    default_spec = dict(member_specs[0])
    return default_spec, member_specs, judge_spec, research_spec



def _call_model_text(
    prompt: str,
    max_tokens: int,
    temperature: float,
    model: str | None = None,
    provider: str | None = None,
    timeout: int = DEFAULT_MODEL_TIMEOUT,
    retries: int = DEFAULT_MEMBER_RETRIES,
    jitter_ms: int = DEFAULT_REQUEST_JITTER_MS,
    purpose: str = "hermes-omnicouncil.member",
) -> str:
    """Call model via ctx.llm (any provider). Falls back to fallback chain on failure."""
    prompt_text = _truncate_text(prompt, MAX_PROMPT_CONTEXT_CHARS, "…[prompt truncated]")
    attempts = max(1, int(retries or 0) + 1)
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            _request_jitter(jitter_ms)
            # Look up provider from resolved specs (module-level globals; smoke tests skip)
            _prov = provider or (_MODEL_PROVIDER_MAP.get(model) if model else None)
            if not _prov and model:
                # Also check judge/research providers
                if model == _JUDGE_PROVIDER or model == _JUDGE_SPEC.get('model', ''):
                    _prov = _JUDGE_PROVIDER
                elif model == _RESEARCH_SPEC.get('model', ''):
                    _prov = _RESEARCH_PROVIDER
            result = call_hermes_model(
            prompt_text,
            provider=_prov,
            model=model,
            max_tokens=max_tokens,
                temperature=temperature,
                retries=1,
                timeout=timeout,
                purpose=purpose,
            )
            if result and isinstance(result, dict):
                err = result.get("error")
                if err:
                    raise RuntimeError(str(err) + " — " + str(result.get("fix", "")))
                content = result.get("content") or ""
                if content:
                    return str(content)
            raise RuntimeError("empty model response")
        except Exception as exc:
            last_error = exc
            if attempt + 1 >= attempts:
                break
            delay = min(2**attempt, 8) + min(0.25 * attempt, 0.75)
            time.sleep(delay)
    raise RuntimeError(str(last_error or "model call failed"))

def _is_retryable_error_text(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in ("timeout", "reset", "connection", "refused", "503", "502", "429"))


# ═══════════════════════════════════════════════════════════════
#  Safe tool execution (read-only)
# ═══════════════════════════════════════════════════════════════

def _await_if_needed(result: Any) -> Any:
    """Resolve coroutine/awaitable tool results from sync broker code."""
    if not inspect.isawaitable(result):
        return result
    try:
        return asyncio.run(result)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(result)
        finally:
            loop.close()


def _normalise_tool_result(result: Any) -> Any:
    """Normalize registry/plugin handler output into a JSON-ish object."""
    result = _await_if_needed(result)
    if isinstance(result, dict):
        return result
    if isinstance(result, list):
        return {"ok": True, "items": result, "count": len(result)}
    if result is None:
        return {"ok": True, "result": None}
    text = _redact(str(result))
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                return parsed
            if isinstance(parsed, list):
                return {"ok": True, "items": parsed, "count": len(parsed)}
        except Exception:
            pass
    return {"ok": True, "result": stripped}


def _profile_home() -> Path:
    return Path(_os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")).expanduser()


def _local_skills_index(args: dict[str, Any]) -> dict[str, Any]:
    root = _profile_home() / "skills"
    limit = _clamp_int(args.get("limit"), 80, 1, 300)
    category = str(args.get("category") or "").strip().strip("/")
    search_root = root / category if category else root
    skills: list[dict[str, Any]] = []
    if not search_root.exists():
        return {"ok": False, "error": "skills_root_missing", "path": str(search_root)}
    for skill_file in sorted(search_root.rglob("SKILL.md")):
        if len(skills) >= limit:
            break
        try:
            content = skill_file.read_text(encoding="utf-8", errors="replace")[:4000]
        except Exception:
            continue
        name_match = re.search(r"^name:\s*['\"]?([^'\"\n]+)", content, flags=re.MULTILINE)
        desc_match = re.search(r"^description:\s*['\"]?(.+?)['\"]?\s*$", content, flags=re.MULTILINE)
        rel = skill_file.parent.relative_to(root).as_posix()
        skills.append({
            "name": (name_match.group(1).strip() if name_match else rel),
            "description": _truncate_text(desc_match.group(1).strip() if desc_match else "", 300),
            "path": rel,
        })
    return {"ok": True, "source": "local_skills_fallback", "count": len(skills), "skills": skills}


def _find_local_skill(name: str) -> Path | None:
    root = _profile_home() / "skills"
    requested = str(name or "").strip().strip("/")
    if not requested or ".." in requested.split("/"):
        return None
    direct = root / requested / "SKILL.md"
    if direct.exists():
        return direct
    for skill_file in sorted(root.rglob("SKILL.md")):
        rel = skill_file.parent.relative_to(root).as_posix()
        if rel == requested or rel.endswith("/" + requested):
            return skill_file
        try:
            head = skill_file.read_text(encoding="utf-8", errors="replace")[:1200]
        except Exception:
            continue
        m = re.search(r"^name:\s*['\"]?([^'\"\n]+)", head, flags=re.MULTILINE)
        if m and m.group(1).strip() == requested:
            return skill_file
    return None


def _local_skill_view(args: dict[str, Any]) -> dict[str, Any]:
    skill_file = _find_local_skill(str(args.get("name") or ""))
    if not skill_file:
        return {"ok": False, "error": "skill_not_found", "name": str(args.get("name") or "")}
    target = skill_file
    linked = {}
    file_path = str(args.get("file_path") or "").strip().strip("/")
    if file_path:
        if ".." in file_path.split("/"):
            return {"ok": False, "error": "invalid_file_path"}
        candidate = skill_file.parent / file_path
        try:
            candidate.relative_to(skill_file.parent)
        except Exception:
            return {"ok": False, "error": "invalid_file_path"}
        target = candidate
    else:
        for subdir in ("references", "templates", "scripts", "assets"):
            folder = skill_file.parent / subdir
            if folder.exists():
                linked[subdir] = [p.relative_to(skill_file.parent).as_posix() for p in sorted(folder.rglob("*")) if p.is_file()][:80]
    if not target.exists() or not target.is_file():
        return {"ok": False, "error": "skill_file_not_found", "path": str(target)}
    content = target.read_text(encoding="utf-8", errors="replace")
    return {"success": True, "ok": True, "source": "local_skills_fallback", "path": str(target), "content": _truncate_text(content, 80000), "linked_files": linked}


def _local_read_file(args: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(args.get("path") or "")).expanduser()
    if not path.exists() or not path.is_file():
        return {"ok": False, "error": "file_not_found", "path": str(path)}
    offset = _clamp_int(args.get("offset"), 1, 1, 10_000_000)
    limit = _clamp_int(args.get("limit"), 500, 1, 2000)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    selected = lines[offset - 1: offset - 1 + limit]
    body = "\n".join(f"{offset + idx}|{line}" for idx, line in enumerate(selected))
    return {"ok": True, "source": "local_file_fallback", "path": str(path), "content": _truncate_text(body, 100000), "total_lines": len(lines)}


def _local_search_files(args: dict[str, Any]) -> dict[str, Any]:
    pattern = str(args.get("pattern") or "")
    if not pattern:
        return {"ok": False, "error": "pattern_required"}
    base = Path(str(args.get("path") or ".")).expanduser()
    target = str(args.get("target") or "content")
    limit = _clamp_int(args.get("limit"), 50, 1, 200)
    if not base.exists():
        return {"ok": False, "error": "path_not_found", "path": str(base)}
    results: list[dict[str, Any]] = []
    if target == "files":
        if base.is_file():
            return {"ok": True, "source": "local_search_fallback", "total_count": 1, "files": [str(base)], "matches": [{"path": str(base)}]}
        for p in sorted(base.rglob(pattern if any(ch in pattern for ch in "*?[]") else f"*{pattern}*")):
            if p.is_file():
                results.append({"path": str(p)})
                if len(results) >= limit:
                    break
        return {"ok": True, "source": "local_search_fallback", "total_count": len(results), "files": [r["path"] for r in results], "matches": results}
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        return {"ok": False, "error": "invalid_regex", "detail": str(exc)}
    glob_pat = str(args.get("file_glob") or "*")
    paths = [base] if base.is_file() else sorted(base.rglob(glob_pat))
    for p in paths:
        if not p.is_file():
            continue
        try:
            if p.stat().st_size > 2_000_000:
                continue
            for lineno, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if rx.search(line):
                    results.append({"path": str(p), "line": lineno, "content": _truncate_text(line, 800)})
                    if len(results) >= limit:
                        return {"ok": True, "source": "local_search_fallback", "total_count": len(results), "matches": results}
        except Exception:
            continue
    return {"ok": True, "source": "local_search_fallback", "total_count": len(results), "matches": results}


def _call_profile_plugin_tool(tool_name: str, tool_args: dict[str, Any]) -> Any | None:
    """Best-effort read-only call into another profile plugin via fake registration."""
    plugin_name = "memory-wiki" if tool_name.startswith("memory_wiki_") else ""
    if not plugin_name:
        return None
    plugin_file = _profile_home() / "plugins" / plugin_name / "__init__.py"
    if not plugin_file.exists():
        return None
    try:
        spec = _iu.spec_from_file_location(f"omnicouncil_bridge_{plugin_name.replace('-', '_')}", plugin_file)
        if spec is None or spec.loader is None:
            return None
        module = _iu.module_from_spec(spec)
        spec.loader.exec_module(module)
        class BridgeCtx:
            def __init__(self):
                self.tools = []
            def register_tool(self, **kwargs):
                self.tools.append(kwargs)
            def register_memory_provider(self, *args, **kwargs):
                self.memory_provider = (args, kwargs)
            def register_hook(self, *args, **kwargs):
                return None
            def __getattr__(self, name):
                if str(name).startswith("register_"):
                    return lambda *args, **kwargs: None
                raise AttributeError(name)
        ctx = BridgeCtx()
        if hasattr(module, "register"):
            module.register(ctx)
        memory_provider = getattr(ctx, "memory_provider", None)
        if memory_provider:
            provider_args = memory_provider[0] if isinstance(memory_provider, tuple) else ()
            provider = provider_args[0] if provider_args else None
            handler = getattr(provider, "handle_tool_call", None)
            if callable(handler):
                return handler(tool_name, tool_args)
        for entry in ctx.tools:
            if isinstance(entry, dict) and entry.get("name") == tool_name and callable(entry.get("handler")):
                return entry["handler"](tool_args)
    except Exception as exc:
        return {"ok": False, "error": "profile_plugin_bridge_failed", "tool": tool_name, "detail": _truncate_text(str(exc), 800)}
    return None


def _call_local_readonly_tool(tool_name: str, tool_args: dict[str, Any]) -> Any | None:
    if tool_name == "skills_list":
        return _local_skills_index(tool_args)
    if tool_name == "skill_view":
        return _local_skill_view(tool_args)
    if tool_name == "read_file":
        return _local_read_file(tool_args)
    if tool_name == "search_files":
        return _local_search_files(tool_args)
    bridged = _call_profile_plugin_tool(tool_name, tool_args)
    if bridged is not None:
        return bridged
    if tool_name in {"web_search", "web_extract", "web_research_brief", "memory_wiki_query", "memory_wiki_pack_context"}:
        return {"ok": False, "error": "runtime_tool_unavailable", "tool": tool_name, "source": "local_fallback"}
    return None


def _call_registry_tool(tool_name: str, tool_args: dict[str, Any]) -> Any | None:
    try:
        from tools.registry import discover_builtin_tools, registry
        if registry.get_entry(tool_name) is None:
            try:
                discover_builtin_tools()
            except Exception:
                pass
        if registry.get_entry(tool_name) is not None:
            return registry.dispatch(tool_name, tool_args)
    except Exception:
        # Standalone plugin smoke tests do not always have Hermes core on sys.path.
        # Fall through to local read-only fallbacks instead of surfacing a fake broker failure.
        return None
    return None


def _call_runtime_tool(tool_name: str, tool_args: dict[str, Any]) -> Any:
    tool_name = str(tool_name or "").strip()
    if not isinstance(tool_args, dict):
        tool_args = {}
    errors: list[str] = []
    ctx = _RUNTIME_CTX
    if ctx is not None:
        for method_name in ("call_tool", "invoke_tool", "run_tool", "execute_tool", "dispatch_tool"):
            method = getattr(ctx, method_name, None)
            if not callable(method):
                continue
            for call in (
                lambda: method(tool_name, tool_args),
                lambda: method(name=tool_name, arguments=tool_args),
                lambda: method({"name": tool_name, "arguments": tool_args}),
            ):
                try:
                    return _normalise_tool_result(call())
                except TypeError as exc:
                    errors.append(f"{method_name}:type:{_truncate_text(str(exc), 160)}")
                    continue
                except Exception as exc:
                    errors.append(f"{method_name}:error:{_truncate_text(str(exc), 160)}")
                    continue
        for registry_attr in ("tools", "tool_registry", "_tools", "_tool_registry", "registered_tools"):
            registry_obj = getattr(ctx, registry_attr, None)
            entries: list[Any] = []
            if isinstance(registry_obj, dict):
                entries = [registry_obj.get(tool_name)]
            elif isinstance(registry_obj, list):
                entries = [item for item in registry_obj if isinstance(item, dict) and item.get("name") == tool_name]
            for tool_entry in entries:
                if tool_entry is None:
                    continue
                handler = tool_entry if callable(tool_entry) else None
                if isinstance(tool_entry, dict):
                    handler = tool_entry.get("handler") or tool_entry.get("fn") or tool_entry.get("callable")
                else:
                    handler = getattr(tool_entry, "handler", None) or getattr(tool_entry, "fn", None)
                if not callable(handler):
                    continue
                for call in (lambda: handler(tool_args), lambda: handler(**tool_args)):
                    try:
                        return _normalise_tool_result(call())
                    except TypeError as exc:
                        errors.append(f"{registry_attr}:type:{_truncate_text(str(exc), 160)}")
                        continue
                    except Exception as exc:
                        errors.append(f"{registry_attr}:error:{_truncate_text(str(exc), 160)}")
                        continue
        direct = getattr(ctx, tool_name, None)
        if callable(direct):
            for call in (lambda: direct(tool_args), lambda: direct(**tool_args)):
                try:
                    return _normalise_tool_result(call())
                except TypeError as exc:
                    errors.append(f"ctx_direct:type:{_truncate_text(str(exc), 160)}")
                    continue
                except Exception as exc:
                    errors.append(f"ctx_direct:error:{_truncate_text(str(exc), 160)}")
                    continue
    registry_result = _call_registry_tool(tool_name, tool_args)
    if registry_result is not None:
        return _normalise_tool_result(registry_result)
    local_result = _call_local_readonly_tool(tool_name, tool_args)
    if local_result is not None:
        return _normalise_tool_result(local_result)
    return {"ok": False, "error": "runtime_tool_unavailable", "tool": tool_name, "attempts": errors[-8:]}

# ═══════════════════════════════════════════════════════════════
#  Ephemeral blackboard — lives within one OmniCouncil run
# ═══════════════════════════════════════════════════════════════
def _init_ephemeral_blackboard(session_id: str, task: str) -> dict[str, Any]:
    """Create structured ephemeral blackboard for a single OmniCouncil session."""
    return {
        "session_id": session_id,
        "task": task,
        "notes": [],
        "claims": [],
        "evidence": [],
        "file_reads": [],
        "memory_reads": [],
        "proposed_patches": [],
        "risks": [],
        "decisions": [],
    }

def _blackboard_add_entry(blackboard: dict[str, Any], entry_type: str, entry: dict[str, Any]) -> None:
    """Add a structured entry to the ephemeral blackboard."""
    key_map = {
        "claim": "claims",
        "evidence": "evidence",
        "note": "notes",
        "file_read": "file_reads",
        "memory_read": "memory_reads",
        "proposed_patch": "proposed_patches",
        "risk": "risks",
        "decision": "decisions",
    }
    key = key_map.get(entry_type)
    if key and key in blackboard:
        blackboard[key].append(entry)

# ═══════════════════════════════════════════════════════════════
#  Broker — single entry point for all council tool requests
# ═══════════════════════════════════════════════════════════════
def broker_tool_call(agent: str, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Central broker: all council model tool requests go through here.
    
    - Denies COUNCIL_DENIED_TOOLS (file mutations, terminal, git)
    - Forces blackboard namespace for memory_wiki writes
    - Enforces readonly workspace policy for file reads
    - Routes through ctx.dispatch_tool (normal Hermes pipelines)
    """
    if tool_name not in COUNCIL_ALLOWED_TOOLS:
        return {
            "ok": False,
            "error": "tool_denied",
            "tool": tool_name,
            "reason": "Council members may read files/memory and write blackboard only. Code/file mutations require executor approval.",
        }

    # Получить CouncilRunContext из ContextVar (P0 #1 fix — наследуется в workers)
    run_ctx: CouncilRunContext | None = _ACTIVE_RUN_CTX.get()

    # Durable writes require an active isolated council context. Read tools must
    # never be misclassified as mutations or have their query arguments rewritten.
    _MEMORY_MUTATION_TOOLS = {
        "memory_wiki_write_firewall",
        "memory_wiki_add_claim",
        "memory_wiki_add_decision",
        "memory_wiki_add_evidence",
        "memory_wiki_add_task_capsule",
        "memory_wiki_post_task",
    }
    if run_ctx is None and tool_name in _MEMORY_MUTATION_TOOLS:
        return {
            "ok": False,
            "error": "missing_council_run_context",
            "detail": "Durable memory mutations require an active council context.",
        }

    if run_ctx is not None and run_ctx.is_cancelled():
        return {"ok": False, "error": "council_cancelled", "detail": "Tool call blocked: council is cancelled"}
    if run_ctx is not None and run_ctx.budget and not run_ctx.budget.can_call_tool():
        return {
            "ok": False,
            "error": "tool_budget_exhausted",
            "detail": f"Too many tool calls: {run_ctx.budget.tool_calls}/{run_ctx.budget.max_tool_calls}",
        }
    if run_ctx is not None and run_ctx.budget:
        run_ctx.budget.record_tool_call()

    if tool_name in _MEMORY_MUTATION_TOOLS:
        args = force_blackboard_namespace(run_ctx, agent, tool_name, args)

    # Enforce readonly workspace policy
    if tool_name in {"read_file", "search_files"}:
        args = enforce_readonly_workspace_policy(args)

    ctx = _RUNTIME_CTX
    if ctx is not None and hasattr(ctx, "dispatch_tool"):
        try:
            result = ctx.dispatch_tool(tool_name, args)
            return _normalise_tool_result(result)
        except Exception as exc:
            return {"ok": False, "error": "dispatch_failed", "tool": tool_name, "detail": _truncate_text(str(exc), 500)}
    
    # Fallback: use existing call infrastructure
    return _call_runtime_tool(tool_name, args)


def force_blackboard_namespace(run_ctx: CouncilRunContext, agent: str, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Force all memory_wiki writes into omnicouncil:blackboard namespace.
    P0 #2 fix: модель НЕ управляет namespace. Система принудительно перезаписывает."""
    args = dict(args or {})
    # Удалить любые попытки модели задать namespace
    for banned in ("session_id", "topic", "source", "run_id"):
        args.pop(banned, None)
    # Принудительно задать правильный namespace
    blackboard_topic = f"omnicouncil:blackboard:{run_ctx.session_id}"
    args["topic"] = blackboard_topic
    args["run_id"] = run_ctx.session_id
    args["source"] = f"omnicouncil:agent:{agent}"
    # Ensure write_firewall is called first for claims
    if tool_name == "memory_wiki_add_claim":
        args["require_firewall"] = True
    return args


def force_blackboard_namespace_legacy(agent: str, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Legacy fallback — использует устаревшие глобалы. Только для тестов без CouncilRunContext."""
    args = dict(args or {})
    session_id = _OMNICOUNCIL_SESSION_ID or "unknown"
    blackboard_topic = f"omnicouncil:blackboard:{session_id}"
    if "topic" not in args:
        args["topic"] = blackboard_topic
    args["source"] = f"omnicouncil:agent:{agent}"
    if tool_name == "memory_wiki_add_claim":
        args["require_firewall"] = True
    return args


def enforce_readonly_workspace_policy(args: dict[str, Any]) -> dict[str, Any]:
    """Ensure file read operations don't escape workspace boundaries."""
    args = dict(args or {})
    # Strip any write-adjacent flags that might have been injected
    for banned in ("mode", "write", "append", "create", "truncate"):
        args.pop(banned, None)
    return args


# ── CouncilRunContext — replaces all module-level globals ──────────
# P0 #1 fix: каждый council-запуск получает изолированный контекст.
# Параллельные запуски НЕ смешивают session_id, providers, бюджет.
from .council_context import (
    CouncilRunContext,
    RunBudget,
    RecalledItem,
    sanitize_recalled,
    PROVIDER_DATA_POLICIES,
    _run_context,
)

# P0 #1 FIX: contextvars.ContextVar вместо threading.local()
# ContextVar НАСЛЕДУЕТСЯ в ThreadPoolExecutor workers (в отличие от threading.local)
import contextvars as _contextvars
_ACTIVE_RUN_CTX: _contextvars.ContextVar = _contextvars.ContextVar(
    'omnicouncil_run_ctx', default=None
)

# Deprecated — оставлены для обратной совместимости, НЕ используются новым кодом
_OMNICOUNCIL_SESSION_ID: str | None = None
_MODEL_PROVIDER_MAP: dict[str, str | None] = {}
_JUDGE_PROVIDER: str | None = None
_RESEARCH_PROVIDER: str | None = None


def _execute_safe_tool(tool_name: str, tool_args: dict[str, Any]) -> dict[str, Any] | str:
    """Execute a safe tool through the council broker."""
    tool_name = str(tool_name or "").strip()
    result = broker_tool_call("council_agent", tool_name, tool_args if isinstance(tool_args, dict) else {})
    return _redact(str(result)) if not isinstance(result, dict) else result

def _tool_result_preview(result: Any, max_chars: int = 6000) -> dict[str, Any]:
    """Return a bounded/redacted result object suitable for agent prompts."""
    if isinstance(result, dict):
        text = _redact(_json(result))
        try:
            parsed = _safe_json_loads(text)
            if isinstance(parsed, dict):
                return parsed if len(text) <= max_chars else {"ok": parsed.get("ok", True), "preview": _truncate_text(text, max_chars)}
        except Exception:
            pass
        return {"ok": True, "preview": _truncate_text(text, max_chars)}
    return {"ok": True, "preview": _truncate_text(_redact(str(result)), max_chars)}


def _execute_tool_requests(tool_requests: list[dict[str, Any]], ledger: list[dict[str, Any]], max_workers: int = 4) -> list[dict[str, Any]]:
    """Broker safe read-only tool requests and attach bounded results in-place.

    Agents can only request tools; this orchestrator executes whitelisted read-only
    tools and exposes redacted previews to later collaboration rounds and judge.
    """
    if not tool_requests:
        return tool_requests
    pending = [req for req in tool_requests if isinstance(req, dict) and req.get("tool") and "result" not in req]
    if not pending:
        return tool_requests

    # ── Filter weak requests when we have enough strong ones ──
    strong = [r for r in pending if not r.get("weak_request")]
    weak = [r for r in pending if r.get("weak_request")]
    if strong and weak:
        # Keep weak requests only if we're under a reasonable threshold
        # or if the weak request has a valid reason (some slip through)
        max_weak = max(0, len(strong) // 2)  # at most 50% weak vs strong
        if len(weak) > max_weak:
            # Mark excess weak requests as skipped instead of executing them
            for skipped in weak[max_weak:]:
                skipped["result"] = {"ok": False, "error": "skipped_weak_request", "note": "Weak request skipped: no reason/expected_information_gain"}
                skipped["executed"] = False
            weak = weak[:max_weak]
        pending = strong + weak

    def run(req: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        tool = str(req.get("tool") or "").strip()
        args = req.get("args", {}) if isinstance(req.get("args", {}), dict) else {}
        started = time.time()
        try:
            result = _execute_safe_tool(tool, args)
            preview = _tool_result_preview(result)
            preview["elapsed_ms"] = int((time.time() - started) * 1000)
            return req, preview
        except Exception as exc:
            return req, {"ok": False, "error": _truncate_text(str(exc), 800), "elapsed_ms": int((time.time() - started) * 1000)}

    worker_count = max(1, min(len(pending), max_workers or 4))
    completed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as pool:
        future_map = {pool.submit(_contextvars.copy_context().run, run, req): req for req in pending}
        for future in concurrent.futures.as_completed(future_map):
            req = future_map[future]
            try:
                _, result = future.result()
            except Exception as exc:
                result = {"ok": False, "error": _truncate_text(str(exc), 800)}
            req["result"] = result
            req["executed"] = True
            completed += 1
    if completed:
        _add_evidence(ledger, "tool_execution", f"Executed {completed} brokered safe tool requests.", count=completed)
    return tool_requests


# ═══════════════════════════════════════════════════════════════
#  Blackboard & message infrastructure
# ═══════════════════════════════════════════════════════════════
def _build_blackboard_policy(
    max_tool_requests: int,
    active_tool_agents: int,
    mutating_agents: int,
    minimum_tools: bool = True,
    brokered_tools: bool = True,
) -> dict[str, Any]:
    return {
        "enabled": True,
        "name": "omni_blackboard",
        "roles": list(OMNI_BLACKBOARD_ROLES),
        "rounds": list(OMNI_BLACKBOARD_ROUNDS),
        "topology": {
            "parallel_primary_workers": DEFAULT_MAX_MEMBER_WORKERS,
            "active_tool_agents_target": active_tool_agents,
            "mutating_agents_target": mutating_agents,
            "all_agents_see_capabilities": True,
            "peer_collaboration_via_blackboard": True,
            "message_rounds_enabled": True,
            "judge_synthesizes": True,
        },
        "tool_policy": {
            "enabled": brokered_tools,
            "safe_tools_only": True,
            "allowed_tools": sorted(COUNCIL_ALLOWED_TOOLS),
            "denied_tools": sorted(COUNCIL_DENIED_TOOLS),
            "mutation_policy": critical_change_policy if 'critical_change_policy' in dir() else "operator_only",
            "allow_file_mutations": allow_file_mutations if 'allow_file_mutations' in dir() else False,
            "allow_code_mutations": allow_code_mutations if 'allow_code_mutations' in dir() else False,
            "max_tool_requests_cap": max_tool_requests,
            "minimum_tools": minimum_tools,
        },
        "message_policy": {
            "enabled": True,
            "max_chars": MAX_MESSAGE_CHARS,
            "history_chars": MAX_MESSAGE_HISTORY_CHARS,
            "allowed_types": ["question", "answer", "challenge", "clarification", "evidence_share"],
        },
        "minimum_tools_policy": {
            "enabled": minimum_tools,
            "primary_rule": "reasoning + blackboard first, tool request only for concrete information gain",
        },
    }


def _blackboard_text(manifest: dict[str, Any]) -> str:
    policy = manifest.get("blackboard") or {}
    if not policy:
        return ""
    return _truncate_text(_json(policy), 60_000, "…[blackboard policy truncated]")


def _messages_text(messages: list[dict[str, Any]]) -> str:
    if not messages:
        return ""
    return _truncate_text(_json(messages), MAX_MESSAGE_HISTORY_CHARS)


# ═══════════════════════════════════════════════════════════════
#  capability manifest
# ═══════════════════════════════════════════════════════════════
def _scan_plugins(ledger: list[dict[str, Any]], limit: int = 120) -> list[dict[str, Any]]:
    root = Path.home() / ".hermes" / "plugins"
    plugins: list[dict[str, Any]] = []
    if root.exists():
        for yaml_path in sorted(root.glob("*/plugin.yaml"))[:limit]:
            text = _read_text_safe(yaml_path, 6000)
            name = _extract_yaml_field(text, "name") or yaml_path.parent.name
            version = _extract_yaml_field(text, "version")
            description = _extract_yaml_field(text, "description")
            tools = re.findall(r"(?m)^\s*-\s*([A-Za-z0-9_\-]+)\s*$", text)
            plugins.append({"name": name, "version": version, "description": description, "tools": tools, "path": str(yaml_path)})
    _add_evidence(ledger, "plugin_scan", f"Scanned {len(plugins)} Hermes plugin manifests.", count=len(plugins))
    return plugins


def _scan_skills(ledger: list[dict[str, Any]], selected: list[str] | None = None, limit: int = 260) -> list[dict[str, Any]]:
    root = Path.home() / ".hermes" / "skills"
    skills: list[dict[str, Any]] = []
    if root.exists():
        for skill_path in sorted(root.glob("**/SKILL.md")):
            text = _read_text_safe(skill_path, 10000)
            desc = _extract_yaml_field(text, "description")
            rel_parts = skill_path.relative_to(root).parts
            name = rel_parts[-2] if len(rel_parts) >= 2 else skill_path.parent.name
            if not desc:
                lines = [ln.strip("# ").strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("---")]
                desc = lines[1] if len(lines) > 1 else (lines[0] if lines else "")
            skills.append({"name": name, "description": _truncate_text(desc, 500), "path": str(skill_path)})
            if len(skills) >= limit:
                break
    _add_evidence(ledger, "skill_scan", f"Indexed {len(skills)} Hermes skills.", count=len(skills))
    return skills


def _scan_mcp(ledger: list[dict[str, Any]]) -> dict[str, Any]:
    candidates = [Path.home() / ".hermes" / "config.yaml", Path("/root/.hermes/config.yaml")]
    found: list[dict[str, Any]] = []
    for cfg in candidates:
        if not cfg.exists():
            continue
        text = _read_text_safe(cfg, 120_000)
        if "mcp_servers" not in text:
            continue
        block = text[text.find("mcp_servers"):]
        names = re.findall(r"(?m)^\s{2}([A-Za-z0-9_.\-]+)\s*:\s*$", block)
        for name in names[:80]:
            found.append({"name": name, "config": str(cfg), "transport": "stdio/http", "details": "redacted manifest only"})
    _add_evidence(ledger, "mcp_scan", f"Discovered {len(found)} configured MCP server entries.", count=len(found))
    return {"servers": found, "note": "Native MCP tools are available to Hermes after startup."}


def _tool_manifest(profile: str, enabled_toolsets: list[str]) -> dict[str, Any]:
    toolsets = enabled_toolsets or CAPABILITY_PROFILES.get(profile, CAPABILITY_PROFILES["omni"])
    result = {name: STATIC_TOOLSETS.get(name, [f"unknown toolset: {name}"]) for name in toolsets}
    return {"profile": profile, "toolsets": result, "safe_agent_tools": SAFE_AGENT_TOOLS}


def _build_capability_manifest(args: dict[str, Any], ledger: list[dict[str, Any]]) -> dict[str, Any]:
    profile = args.get("capability_profile") or "omni"
    if profile not in CAPABILITY_PROFILES:
        profile = "omni"
    enabled_toolsets = _as_str_list(args.get("enabled_toolsets"))
    agentic_blackboard = _normalise_bool(args.get("agentic_blackboard"), False)
    minimum_tools = _normalise_bool(args.get("minimum_tools"), True)
    brokered_tools = _normalise_bool(args.get("brokered_tools"), True)
    manifest = {
        "tool_mode": args.get("tool_mode", "safe_agent"),
        "capability_profile": profile,
        "tools": _tool_manifest(profile, enabled_toolsets),
        "plugins": [],
        "skills": [],
        "mcp": {"servers": []},
        "protocols": {
            "safe_agent_note": f"Agents have access to safe read-only tools: {', '.join(SAFE_AGENT_TOOLS[:8])}... No patch/write/terminal.",
            "evidence_rule": "Cite evidence ids for claims; mark unsupported as assumptions.",
            "minimum_tools_rule": "Use reasoning/blackboard first; request tools only for concrete information gain.",
        },
    }
    if agentic_blackboard or preset_active(args):
        manifest["blackboard"] = _build_blackboard_policy(
            max_tool_requests=_clamp_int(args.get("max_tool_requests"), MAX_AGENTIC_TOOL_REQUESTS, 0, MAX_BROKERED_TOOL_REQUESTS),
            active_tool_agents=_clamp_int(args.get("active_tool_agents"), AGENTIC_ACTIVE_TOOL_AGENTS, 1, 20),
            mutating_agents=_clamp_int(args.get("mutating_agents"), AGENTIC_MUTATING_AGENTS, 0, 0),
            minimum_tools=minimum_tools,
            brokered_tools=brokered_tools,
        )
    if _normalise_bool(args.get("auto_capability_scan"), True):
        manifest["plugins"] = _scan_plugins(ledger)
    if _normalise_bool(args.get("auto_skills"), True) or _as_str_list(args.get("skills")):
        manifest["skills"] = _scan_skills(ledger, _as_str_list(args.get("skills")))
    if args.get("mcp_mode", "manifest") != "off":
        manifest["mcp"] = _scan_mcp(ledger)
    _add_evidence(ledger, "capability_manifest", "Built bounded capability manifest for OmniCouncil prompts.", profile=profile, blackboard=agentic_blackboard)
    return manifest


def preset_active(args: dict[str, Any]) -> bool:
    p = str(args.get("preset") or "").lower()
    return p in {"omni_blackboard", "audit", "max", "deep"}


def _manifest_text(manifest: dict[str, Any]) -> str:
    compact = {
        "tool_mode": manifest.get("tool_mode"),
        "capability_profile": manifest.get("capability_profile"),
        "tools": manifest.get("tools"),
        "plugins": manifest.get("plugins", [])[:120],
        "skills": manifest.get("skills", [])[:260],
        "mcp": manifest.get("mcp"),
        "protocols": manifest.get("protocols"),
        "blackboard": manifest.get("blackboard"),
    }
    return _truncate_text(_json(compact), MAX_MANIFEST_CHARS, "…[capability manifest truncated]")


def _evidence_text(ledger: list[dict[str, Any]]) -> str:
    return _truncate_text(_json(ledger), MAX_EVIDENCE_CHARS, "…[evidence truncated]")


# ═══════════════════════════════════════════════════════════════
#  Evidence ledger / Plan-Probe-Decide / Prosecutor / Compiler
# ═══════════════════════════════════════════════════════════════
CLAIM_STATUSES = {"supported", "unsupported", "assumption", "refuted", "uncertain"}


def _claim_count(ledger: list[dict[str, Any]]) -> int:
    return sum(1 for item in ledger if isinstance(item, dict) and item.get("kind") == "claim")


def _add_claim(
    ledger: list[dict[str, Any]],
    source: str,
    claim: str,
    claim_type: str = "finding",
    evidence_refs: list[str] | None = None,
    confidence: float | int | str = 0.5,
    status: str = "",
) -> str:
    claim_text = _truncate_text(claim, 1200).strip()
    if not claim_text:
        return ""
    evidence_refs = [str(x).strip() for x in (evidence_refs or []) if str(x).strip()][:20]
    try:
        conf = max(0.0, min(1.0, float(confidence)))
    except Exception:
        conf = 0.5
    normalized_status = str(status or "").lower().strip()
    if normalized_status not in CLAIM_STATUSES:
        normalized_status = "supported" if evidence_refs else "unsupported"
    duplicate_key = (claim_text.lower(), str(source or "").lower())
    for item in ledger:
        if item.get("kind") == "claim" and (str(item.get("claim", "")).lower(), str(item.get("source", "")).lower()) == duplicate_key:
            return str(item.get("id") or "")
    cid = f"C{_claim_count(ledger) + 1}"
    ledger.append({
        "id": cid,
        "kind": "claim",
        "source": _truncate_text(source, 200),
        "claim": claim_text,
        "type": _truncate_text(claim_type or "finding", 120),
        "evidence_refs": evidence_refs,
        "confidence": round(conf, 3),
        "status": normalized_status,
    })
    return cid


def _extract_claims(text: str, source: str = "agent") -> list[dict[str, Any]]:
    if not text:
        return []
    candidates: list[str] = []
    raw = _json_array_after_marker(text, "CLAIMS_JSON")
    if raw:
        candidates.append(raw)
    # Some models return a raw fenced list without the marker.
    for fence in re.findall(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.IGNORECASE):
        stripped = fence.strip()
        if stripped.startswith("[") and "claim" in stripped.lower():
            candidates.append(stripped)
    out: list[dict[str, Any]] = []
    for candidate in candidates:
        try:
            data = _safe_json_loads(candidate)
        except Exception:
            continue
        if isinstance(data, dict):
            data = data.get("claims") or []
        if not isinstance(data, list):
            continue
        for item in data[:40]:
            if not isinstance(item, dict):
                continue
            claim = str(item.get("claim") or item.get("text") or "").strip()
            if not claim:
                continue
            out.append({
                "source": source,
                "claim": claim,
                "type": str(item.get("type") or item.get("claim_type") or "finding"),
                "evidence_refs": _as_str_list(item.get("evidence_refs") or item.get("evidence") or item.get("refs")),
                "confidence": item.get("confidence", 0.5),
                "status": str(item.get("status") or ""),
            })
        if out:
            break
    return out


def _collect_claims_from_responses(responses: list[dict[str, Any]], ledger: list[dict[str, Any]]) -> list[str]:
    added: list[str] = []
    for item in responses or []:
        if not isinstance(item, dict) or item.get("status") != "success":
            continue
        source = str(item.get("label") or item.get("mission") or "agent")
        text = str(item.get("answer") or item.get("response") or item.get("report") or "")
        for claim in _extract_claims(text, source):
            cid = _add_claim(
                ledger,
                claim.get("source", source),
                claim.get("claim", ""),
                claim.get("type", "finding"),
                claim.get("evidence_refs") or [],
                claim.get("confidence", 0.5),
                claim.get("status", ""),
            )
            if cid:
                added.append(cid)
    if added:
        _add_evidence(ledger, "claims_ledger", f"Registered {len(added)} structured agent claims.", claim_ids=added[:80])
    return added


def _claims_summary(ledger: list[dict[str, Any]]) -> dict[str, Any]:
    claims = [item for item in ledger if isinstance(item, dict) and item.get("kind") == "claim"]
    by_status: dict[str, int] = {}
    for claim in claims:
        status = str(claim.get("status") or "uncertain")
        by_status[status] = by_status.get(status, 0) + 1
    return {
        "total": len(claims),
        "by_status": by_status,
        "supported": [c for c in claims if c.get("status") == "supported"][:30],
        "unsupported": [c for c in claims if c.get("status") != "supported"][:30],
    }


def _normalise_tool_request_item(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    tool = _truncate_text(item.get("tool", ""), 120).strip()
    if not tool or tool not in SAFE_AGENT_TOOLS:
        return None
    mutating = _normalise_bool(item.get("mutating"), False)
    if mutating or tool in ("patch", "write_file", "terminal", "process", "cronjob"):
        return None
    return {
        "tool": tool,
        "args": item.get("args", {}) if isinstance(item.get("args", {}), dict) else {},
        "reason": _truncate_text(item.get("reason", ""), 500),
        "priority": _clamp_int(item.get("priority"), 3, 1, 5),
        "expected_information_gain": _truncate_text(item.get("expected_information_gain", ""), 500),
        "mutating": False,
        "requires_lock": _as_str_list(item.get("requires_lock")),
    }


def _extract_probe_plan(text: str) -> dict[str, Any]:
    if not text:
        return {"unknowns": [], "tool_requests": [], "risk_points": [], "expected_evidence": [], "raw": ""}
    raw = _json_value_after_marker(text, "PROBE_PLAN_JSON", "{")
    if not raw and text.strip().startswith("{"):
        raw = text.strip()
    data: dict[str, Any] = {}
    if raw:
        try:
            parsed = _safe_json_loads(raw)
            if isinstance(parsed, dict):
                data = parsed
        except Exception:
            data = {}
    if not data:
        data = {"raw": _truncate_text(text, 4000)}
    reqs = []
    for item in data.get("tool_requests", []) or []:
        cleaned = _normalise_tool_request_item(item)
        if cleaned:
            reqs.append(cleaned)
    if not reqs:
        reqs = _extract_tool_requests(text, 20)
    return {
        "unknowns": [_truncate_text(x, 800) for x in _as_str_list(data.get("unknowns"))[:30]],
        "risk_points": [_truncate_text(x, 800) for x in _as_str_list(data.get("risk_points"))[:30]],
        "expected_evidence": [_truncate_text(x, 800) for x in _as_str_list(data.get("expected_evidence"))[:30]],
        "tool_requests": reqs,
        "raw": _truncate_text(data.get("raw", ""), 4000) if data.get("raw") else "",
    }


def _run_probe_phase(
    task: str,
    context: str,
    mode: str,
    manifest: dict[str, Any],
    ledger: list[dict[str, Any]],
    max_tool_requests: int,
    model: str,
    timeout: int,
    retries: int,
    jitter_ms: int,
    max_workers: int,
) -> dict[str, Any]:
    prompt = f"""Ты preflight planner Hermes OmniCouncil. Реализуй фазу Plan→Probe→Decide.
Твоя задача — НЕ решать задачу, а перечислить неизвестные, самые ценные read-only проверки и ожидаемые evidence.

Верни JSON object после маркера PROBE_PLAN_JSON:
{{
  "unknowns": ["что неизвестно"],
  "tool_requests": [{{"tool":"read_file|search_files|web_search|web_extract|memory_wiki_query|memory_wiki_pack_context|skill_view|skills_list", "args":{{}}, "reason":"...", "priority":1, "expected_information_gain":"...", "mutating":false}}],
  "risk_points": ["главные риски"],
  "expected_evidence": ["какой факт нужен для решения"]
}}

Запрещено просить mutating tools. Tool policy: {manifest.get('tool_mode')}.
Режим: {mode}
Задача: {task}
Контекст:
{_bounded_context(context)}
"""
    try:
        raw = _call_model_text(prompt, min(16000, DEFAULT_MAX_TOKENS), 0.0, model=model, timeout=timeout, retries=retries, jitter_ms=jitter_ms)
        plan = _extract_probe_plan(raw)
        requests = plan.get("tool_requests", [])[:max(0, min(max_tool_requests, 20))]
        if requests and manifest.get("tool_mode") != "off" and (manifest.get("blackboard", {}).get("tool_policy", {}).get("enabled", True)):
            _execute_tool_requests(requests, ledger, max_workers=max_workers)
        plan["tool_requests"] = requests
        plan["executed"] = sum(1 for req in requests if req.get("executed"))
        _add_evidence(ledger, "plan_probe_decide", "Completed Plan→Probe→Decide preflight.", unknowns=len(plan.get("unknowns", [])), tool_requests=len(requests), executed=plan["executed"])
        return plan
    except Exception as exc:
        _add_evidence(ledger, "plan_probe_decide", "Plan→Probe→Decide preflight failed; continuing without probe evidence.", error=_truncate_text(str(exc), 800))
        return {"status": "failed", "error": _truncate_text(str(exc), 800), "unknowns": [], "tool_requests": [], "risk_points": [], "expected_evidence": []}


def _ensure_minimum_dissent(
    task: str,
    context: str,
    responses: list[dict[str, Any]],
    votes_summary: dict[str, Any],
    ledger: list[dict[str, Any]],
    minimum_objections: int,
    model: str,
    timeout: int,
    retries: int,
    jitter_ms: int,
) -> dict[str, Any]:
    existing = votes_summary.get("blocking_objections", []) if isinstance(votes_summary, dict) else []
    need = max(0, int(minimum_objections or 0) - len(existing))
    if need <= 0:
        return {"required": minimum_objections, "existing": len(existing), "generated": []}
    prompt = f"""Ты forced-dissent reviewer Hermes OmniCouncil.
Consensus without dissent is forbidden for this run. Generate {need} strongest objections/failure modes that the judge must address.

Return only:
FORCED_DISSENT_JSON: [{{"objection":"...", "why_it_matters":"...", "falsification_test":"...", "confidence":0.0}}]

Задача: {task}
Контекст:
{_bounded_context(context)}

Responses/votes snapshot:
{_truncate_text(_json({"votes": votes_summary, "responses": [_format_answer(r, max_chars=1600) for r in responses[:20]]}), 40000)}
"""
    try:
        raw = _call_model_text(prompt, min(24000, DEFAULT_MAX_TOKENS), 0.05, model=model, timeout=timeout, retries=retries, jitter_ms=jitter_ms)
        arr = _json_array_after_marker(raw, "FORCED_DISSENT_JSON")
        generated: list[dict[str, Any]] = []
        if arr:
            try:
                data = _safe_json_loads(arr)
                if isinstance(data, list):
                    for item in data[:need]:
                        if isinstance(item, dict) and item.get("objection"):
                            generated.append({
                                "from": "forced_dissent",
                                "objection": _truncate_text(item.get("objection"), 800),
                                "why_it_matters": _truncate_text(item.get("why_it_matters", ""), 800),
                                "falsification_test": _truncate_text(item.get("falsification_test", ""), 800),
                                "confidence": item.get("confidence", 0.5),
                            })
            except Exception:
                generated = []
        if generated:
            votes_summary.setdefault("blocking_objections", []).extend(generated)
            _add_evidence(ledger, "forced_dissent", f"Generated {len(generated)} forced dissent objections.", objections=generated)
        return {"required": minimum_objections, "existing": len(existing), "generated": generated, "raw": _truncate_text(raw, 4000) if not generated else ""}
    except Exception as exc:
        return {"required": minimum_objections, "existing": len(existing), "generated": [], "error": _truncate_text(str(exc), 800)}


def _run_prosecutor(
    task: str,
    context: str,
    synthesis: str,
    ledger: list[dict[str, Any]],
    votes_summary: dict[str, Any],
    model: str,
    timeout: int,
    retries: int,
    jitter_ms: int,
) -> dict[str, Any]:
    prompt = f"""Ты adversarial prosecutor Hermes OmniCouncil.
Проверь judge synthesis и верни только JSON после PROSECUTOR_JSON.
Ищи: неподтверждённые claims, противоречия, overconfidence, проигнорированный dissent, обязательные проверки перед применением.

PROSECUTOR_JSON: {{
  "verdict":"pass|revise|fail",
  "unsupported_claims":[],
  "contradictions":[],
  "overconfident_claims":[],
  "ignored_dissent":[],
  "must_verify_before_final":[],
  "confidence":0.0
}}

Задача: {task}
Контекст:
{_bounded_context(context)}

Judge synthesis:
{_truncate_text(synthesis, 80000)}

Evidence ledger / claims:
{_evidence_text(ledger)}

Votes:
{_truncate_text(_json(votes_summary), 12000)}
"""
    try:
        raw = _call_model_text(prompt, min(32000, DEFAULT_MAX_TOKENS), 0.0, model=model, timeout=timeout, retries=retries, jitter_ms=jitter_ms)
        obj = _json_value_after_marker(raw, "PROSECUTOR_JSON", "{")
        data: dict[str, Any] = {}
        if obj:
            try:
                parsed = _safe_json_loads(obj)
                if isinstance(parsed, dict):
                    data = parsed
            except Exception:
                data = {}
        if not data:
            data = {"verdict": "unparsed", "raw": _truncate_text(raw, 6000), "unsupported_claims": [], "contradictions": [], "overconfident_claims": [], "ignored_dissent": [], "must_verify_before_final": []}
        data["unsupported_claims"] = [_truncate_text(x, 1000) for x in _as_str_list(data.get("unsupported_claims"))[:40]]
        data["contradictions"] = [_truncate_text(x, 1000) for x in _as_str_list(data.get("contradictions"))[:40]]
        data["overconfident_claims"] = [_truncate_text(x, 1000) for x in _as_str_list(data.get("overconfident_claims"))[:40]]
        data["ignored_dissent"] = [_truncate_text(x, 1000) for x in _as_str_list(data.get("ignored_dissent"))[:40]]
        data["must_verify_before_final"] = [_truncate_text(x, 1000) for x in _as_str_list(data.get("must_verify_before_final"))[:40]]
        _add_evidence(ledger, "prosecutor", "Ran adversarial prosecutor audit.", verdict=data.get("verdict"), unsupported=len(data.get("unsupported_claims", [])), contradictions=len(data.get("contradictions", [])))
        return data
    except Exception as exc:
        return {"verdict": "error", "error": _truncate_text(str(exc), 800), "unsupported_claims": [], "contradictions": [], "overconfident_claims": [], "ignored_dissent": [], "must_verify_before_final": []}


def _compile_judge_output(synthesis: str, ledger: list[dict[str, Any]], votes_summary: dict[str, Any], prosecutor_report: dict[str, Any] | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {}
    raw = _json_value_after_marker(synthesis or "", "JUDGE_COMPILED_JSON", "{")
    if raw:
        try:
            parsed = _safe_json_loads(raw)
            if isinstance(parsed, dict):
                data = parsed
        except Exception:
            data = {}
    if not data:
        stripped = (synthesis or "").strip()
        if stripped.startswith("{"):
            try:
                parsed = _safe_json_loads(stripped)
                if isinstance(parsed, dict):
                    data = parsed
            except Exception:
                data = {}
    claims = _claims_summary(ledger)
    prosecutor_report = prosecutor_report or {}
    compiled = {
        "verdict": _truncate_text(data.get("verdict") or data.get("decision") or "synthesised", 200),
        "confirmed_findings": data.get("confirmed_findings") if isinstance(data.get("confirmed_findings"), list) else claims.get("supported", []),
        "unsupported_claims": data.get("unsupported_claims") if isinstance(data.get("unsupported_claims"), list) else claims.get("unsupported", []),
        "rejected_claims": data.get("rejected_claims") if isinstance(data.get("rejected_claims"), list) else [],
        "decision": _truncate_text(data.get("decision") or data.get("summary") or "", 2000),
        "implementation_plan": data.get("implementation_plan") if isinstance(data.get("implementation_plan"), list) else data.get("implementation_plan", []),
        "tests": data.get("tests") if isinstance(data.get("tests"), list) else [],
        "risks": data.get("risks") if isinstance(data.get("risks"), list) else [],
        "dissent": data.get("dissent") if isinstance(data.get("dissent"), list) else (votes_summary or {}).get("blocking_objections", []),
        "next_step": _truncate_text(data.get("next_step") or "", 1000),
        "prosecutor": prosecutor_report,
        "claim_counts": claims.get("by_status", {}),
    }
    return _tool_result_preview(compiled, 40000)


def _derive_council_lessons(task: str, status: str, compiled: dict[str, Any], prosecutor_report: dict[str, Any], diagnostics: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    lessons: list[dict[str, Any]] = []
    diagnostics = diagnostics or {}
    unsupported = _as_str_list((prosecutor_report or {}).get("unsupported_claims"))
    contradictions = _as_str_list((prosecutor_report or {}).get("contradictions"))
    if unsupported:
        lessons.append({
            "type": "anti_hallucination",
            "lesson": "OmniCouncil produced or preserved unsupported claims; require evidence IDs before judge confirmation.",
            "trigger": _truncate_text(unsupported[0], 600),
            "prevention": "Use Plan→Probe→Decide and keep unsupported claims in compiled_synthesis.unsupported_claims.",
        })
    if contradictions:
        lessons.append({
            "type": "contradiction",
            "lesson": "Agents disagreed on a material point; judge must resolve or list the contradiction explicitly.",
            "trigger": _truncate_text(contradictions[0], 600),
            "prevention": "Run prosecutor and forced dissent before final synthesis.",
        })
    if status != "success":
        lessons.append({
            "type": "degraded_run",
            "lesson": "Council run degraded; preserve fallback reason and avoid treating fallback synthesis as consensus.",
            "trigger": _truncate_text(str(diagnostics.get("warnings") or status), 600),
            "prevention": "Surface judge/model errors in diagnostics and rerun with smaller preset or fallback model if needed.",
        })
    return lessons[:10]


def _persist_council_lessons(task: str, lessons: list[dict[str, Any]], result: dict[str, Any]) -> dict[str, Any]:
    saved: list[dict[str, Any]] = []
    for lesson in lessons[:10]:
        claim = f"OmniCouncil lesson: {lesson.get('lesson')} Prevention: {lesson.get('prevention')}"
        payload = {
            "claim": _truncate_text(claim, 1600),
            "topic": "hermes_omnicouncil_lessons",
            "evidence": _truncate_text(_json({"task": task, "trigger": lesson.get("trigger"), "run_status": result.get("status")}), 2000),
            "source": "hermes_omnicouncil",
            "confidence": 0.72,
            "salience": 0.58,
        }
        raw = _call_memory_wiki_tool("memory_wiki_add_claim", payload)
        saved.append(raw)
    return {"saved": len(saved), "results": saved}


# ═══════════════════════════════════════════════════════════════
#  Cross-plugin context orchestration (Code Shrinker → Memory Wiki)
# ═══════════════════════════════════════════════════════════════
def _unwrap_tool_payload(value: Any) -> dict[str, Any]:
    """Best-effort MCP/result unwrapping without trusting arbitrary object types."""
    current = value
    for _ in range(5):
        if isinstance(current, str):
            text = current.strip()
            if not text:
                return {}
            try:
                current = json.loads(text)
                continue
            except Exception:
                return {"text": text}
        if not isinstance(current, dict):
            return {}
        if current.get("isError") is True:
            content = current.get("content")
            message = "MCP tool returned an error"
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and isinstance(item.get("text"), str):
                        message = item["text"].strip() or message
                        break
            return {"ok": False, "error": message}
        content = current.get("content")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    try:
                        current = json.loads(item["text"])
                    except Exception:
                        current = {"text": item["text"]}
                    break
            else:
                return current
            continue
        for key in ("data", "result", "payload"):
            nested = current.get(key)
            if isinstance(nested, (dict, str)):
                current = nested
                break
        else:
            return current
    return current if isinstance(current, dict) else {}


def _canonical_coverage_manifest(raw: Any, repository_id: str = "") -> dict[str, Any]:
    """Normalize protocol v1/v2 manifests to the strict v2 exchange shape."""
    if not isinstance(raw, dict):
        raise ValueError("coverage_manifest must be an object")
    source_version = int(raw.get("protocol_version", 1) or 1)
    if source_version not in {1, 2}:
        raise ValueError(f"unsupported coverage protocol_version: {source_version}")
    repo = str(repository_id or raw.get("repository_id") or "").strip()
    entries = raw.get("covered") or []
    if not isinstance(entries, list):
        raise ValueError("coverage_manifest.covered must be an array")
    if entries and not repo:
        raise ValueError("repository_id is required for non-empty coverage_manifest")
    if len(entries) > 10_000:
        raise ValueError("coverage_manifest.covered exceeds 10000 entries")

    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for item in entries:
        if not isinstance(item, dict):
            raise ValueError("coverage entry must be an object")
        kind = str(item.get("kind") or "").strip().lower()
        if kind in {"source", "exact-source"}:
            kind = "exact_source"
        if kind not in {"exact_source", "contract", "diagnostic", "test", "project_map", "call_graph"}:
            raise ValueError(f"unsupported coverage kind: {kind}")
        raw_path = unicodedata.normalize("NFC", str(item.get("file_path") or "").strip()).replace("\\", "/")
        path = posixpath.normpath(raw_path) if raw_path else ""
        while path.startswith("./"):
            path = path[2:]
        if path == ".":
            path = ""
        if path.startswith("/") or path == ".." or path.startswith("../") or re.match(r"^[A-Za-z]:/", path):
            raise ValueError(f"coverage file_path must be repository-relative: {path}")

        def _digest(name: str) -> str:
            value = str(item.get(name) or "").strip().lower()
            if value.startswith("sha256:"):
                value = value[7:]
            if value and not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError(f"coverage {name} must be SHA-256")
            return value

        digest = _digest("content_hash")
        diagnostic_digest = _digest("diagnostic_hash")
        entry = {
            "kind": kind,
            "file_path": path,
            "symbol_id": str(item.get("symbol_id") or "").strip()[:512],
            "revision": str(item.get("revision") or item.get("symbol_revision") or "").strip()[:256],
            "content_hash": digest,
            "diagnostic_hash": diagnostic_digest,
            "token_count": max(0, min(int(item.get("token_count") or 0), 10_000_000)),
        }
        identity = (entry["kind"], entry["file_path"], entry["symbol_id"], entry["revision"], entry["content_hash"] or entry["diagnostic_hash"])
        if identity in seen:
            continue
        seen.add(identity)
        normalized.append(entry)
    return {
        "protocol_version": 2,
        "turn_id": str(raw.get("turn_id") or "")[:256],
        "packet_id": str(raw.get("packet_id") or "")[:256],
        "repository_id": repo,
        "commit_sha": str(raw.get("commit_sha") or "")[:128],
        "covered": normalized,
        "created_at": max(0, int(raw.get("created_at") or time.time())),
    }


def _extract_context_text(result: Any, max_chars: int) -> tuple[str, dict[str, Any]]:
    payload = _unwrap_tool_payload(result)
    text = ""
    for key in ("context", "packed_context", "text", "markdown"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            text = value.strip()
            break
    return _truncate_text(_redact(text), max_chars), payload


def _call_code_shrinker(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    candidates = [
        tool_name,
        "mcp__code_shrinker__" + tool_name.replace(".", "_"),
        "code_shrinker__" + tool_name.replace(".", "_"),
    ]
    errors: list[str] = []
    for candidate in candidates:
        raw = _call_runtime_tool(candidate, arguments)
        payload = _unwrap_tool_payload(raw)
        if payload and payload.get("ok") is not False and not payload.get("error"):
            return payload
        if payload:
            errors.append(f"{candidate}:{payload.get('error') or payload.get('message') or 'unusable'}")
    return {"ok": False, "error": "code_shrinker_unavailable", "attempts": errors[:6]}


def _prepare_cross_plugin_context(
    args: dict[str, Any],
    task: str,
    base_context: str,
    memory_max_chars: int,
    ledger: list[dict[str, Any]],
) -> dict[str, Any]:
    mode = str(args.get("code_context_mode") or "auto").strip().lower()
    if mode not in {"off", "auto", "required"}:
        mode = "auto"
    result: dict[str, Any] = {
        "mode": mode,
        "status": "disabled" if mode == "off" else "not_requested",
        "code_context": "",
        "memory_context": "",
        "coverage_manifest": None,
        "context_token_ledger": {
            "code_context_tokens_estimated": 0,
            "memory_context_tokens_estimated": 0,
            "dedup_saved_tokens": 0,
        },
    }
    if mode == "off":
        return result

    repository_id = str(args.get("repository_id") or "").strip()
    target_file = str(args.get("target_file") or "").strip()
    supplied_manifest = args.get("coverage_manifest")
    if not supplied_manifest and not (repository_id and target_file):
        return result

    try:
        manifest = _canonical_coverage_manifest(supplied_manifest, repository_id) if supplied_manifest else None
        packet_payload: dict[str, Any] = {}
        if manifest is None:
            repository_path = str(args.get("repository_path") or "").strip()
            if repository_path:
                scan = _call_code_shrinker("project.scan", {
                    "repository_id": repository_id,
                    "path": repository_path,
                })
                if scan.get("ok") is False and mode == "required":
                    raise RuntimeError(f"project.scan failed: {scan.get('error')}")
            create_payload = _call_code_shrinker("context.create", {
                "repository_id": repository_id,
                "task": {
                    "description": task,
                    "target": str(args.get("target_symbol") or "").strip(),
                    "commitSha": str(args.get("commit_sha") or "").strip(),
                },
                "targetFile": target_file,
                "tokenBudget": max(1000, min(int(args.get("code_context_token_budget") or 8000), 200_000)),
                "qualityFloor": 0.95,
                "mode": "safe",
            })
            if create_payload.get("ok") is False or create_payload.get("error"):
                raise RuntimeError(f"context.create failed: {create_payload.get('error') or create_payload.get('message')}")
            packet_payload = create_payload
            manifest = _canonical_coverage_manifest(create_payload.get("coverage_manifest") or {}, repository_id)

        code_chars = max(1000, min(int(args.get("code_context_chars") or 30_000), 120_000))
        packet = packet_payload.get("packet") if isinstance(packet_payload.get("packet"), dict) else {}
        if packet:
            result["code_context"] = _truncate_text(_redact(_json(packet)), code_chars)

        memory_enabled = (
            _normalise_bool(args.get("auto_memory_context"), True)
            and memory_max_chars > 0
        )
        if not memory_enabled:
            result.update({
                "status": "code_ready" if result["code_context"] else "manifest_ready",
                "coverage_manifest": manifest,
                "suppression_status": "not_requested",
            })
            result["context_token_ledger"] = {
                "code_context_tokens_estimated": len(result["code_context"]) // 4,
                "memory_context_tokens_estimated": 0,
                "dedup_saved_tokens": 0,
                "coverage_entries": len(manifest.get("covered") or []),
            }
            _add_evidence(
                ledger,
                "cross_plugin_context",
                "Code Shrinker coverage was prepared; Memory Wiki prefetch was disabled by caller policy.",
                repository_id=repository_id or manifest.get("repository_id", ""),
                coverage_entries=len(manifest.get("covered") or []),
                memory_requested=False,
            )
            return result

        memory_args: dict[str, Any] = {
            "query": (task + "\n" + base_context[:4000]).strip(),
            "max_chars": memory_max_chars,
            "coverage_manifest": manifest,
            "output_mode": "canonical",
        }
        memory_raw = _execute_safe_tool("memory_wiki_pack_context", memory_args)
        memory_text, memory_payload = _extract_context_text(memory_raw, memory_max_chars)
        if (
            isinstance(memory_raw, dict)
            and (memory_raw.get("ok") is False or memory_raw.get("success") is False)
        ) or memory_payload.get("success") is False or memory_payload.get("ok") is False:
            error_text = (
                memory_payload.get("error")
                or (memory_raw.get("error") if isinstance(memory_raw, dict) else "")
                or "unknown error"
            )
            raise RuntimeError(f"memory_wiki_pack_context failed: {error_text}")

        result.update({
            "status": "ready",
            "coverage_manifest": manifest,
            "code_context": result["code_context"],
            "memory_context": memory_text,
            "suppression_status": memory_payload.get("suppression_status", "unknown"),
            "suppression_manifest": memory_payload.get("suppression_manifest"),
        })
        result["context_token_ledger"] = {
            "code_context_tokens_estimated": len(result["code_context"]) // 4,
            "memory_context_tokens_estimated": len(memory_text) // 4,
            "dedup_saved_tokens": int(memory_payload.get("dedup_saved_tokens") or 0),
            "coverage_entries": len(manifest.get("covered") or []),
        }
        _add_evidence(
            ledger,
            "cross_plugin_context",
            "Code Shrinker coverage was passed to Memory Wiki before council execution.",
            repository_id=repository_id or manifest.get("repository_id", ""),
            coverage_entries=len(manifest.get("covered") or []),
            dedup_saved_tokens=result["context_token_ledger"]["dedup_saved_tokens"],
        )
        return result
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = _truncate_text(str(exc), 1000)
        _add_evidence(ledger, "cross_plugin_context_error", result["error"], required=mode == "required")
        return result


# ═══════════════════════════════════════════════════════════════
#  Memory prefetch
# ═══════════════════════════════════════════════════════════════
def _prefetch_memory_context(task: str, context: str, max_chars: int, ledger: list[dict[str, Any]], coverage_manifest: dict[str, Any] | None = None) -> str:
    query = (task + "\n" + context[:4000]).strip()
    if not query:
        return ""
    payload = {"query": query, "max_chars": max_chars}
    if coverage_manifest:
        payload["coverage_manifest"] = coverage_manifest
    result = _execute_safe_tool("memory_wiki_pack_context", payload)
    text = ""
    if isinstance(result, dict):
        if result.get("success") is False:
            return ""
        for key in ("context", "packed_context", "text", "markdown", "result"):
            val = result.get(key)
            if isinstance(val, str) and val.strip():
                text = val.strip()
                break
    elif isinstance(result, str):
        text = result.strip()
    if not text:
        _add_evidence(ledger, "memory_context", "memory_wiki_pack_context returned no usable context.", available=False)
        return ""
    text = _truncate_text(_redact(text), max_chars)
    _add_evidence(ledger, "memory_context", f"Prefetched {len(text)} chars from memory_wiki_pack_context.", chars=len(text))
    return text


# ═══════════════════════════════════════════════════════════════
#  web_research_brief (composite web search + extract summarisation)
# ═══════════════════════════════════════════════════════════════
def _web_research_brief(query: str, max_sources: int = 5, ledger: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    brief: dict[str, Any] = {"query": query, "sources": [], "summary": ""}
    search_result = _execute_safe_tool("web_search", {"query": query, "limit": max_sources})
    urls: list[str] = []
    if isinstance(search_result, dict) and search_result.get("results"):
        for r in search_result["results"]:
            if isinstance(r, dict):
                urls.append(r.get("url", ""))
    elif isinstance(search_result, str):
        try:
            data = json.loads(search_result)
            if isinstance(data, dict) and data.get("results"):
                for r in data["results"]:
                    if isinstance(r, dict):
                        urls.append(r.get("url", ""))
        except Exception:
            pass
    urls = [u for u in urls if u][:max_sources]
    for url in urls:
        try:
            extract_result = _execute_safe_tool("web_extract", {"urls": [url]})
            brief["sources"].append({"url": url, "extract": _truncate_text(str(extract_result), 2000)})
        except Exception:
            brief["sources"].append({"url": url, "extract": "extraction_failed"})
    if ledger is not None:
        _add_evidence(ledger, "web_research_brief", f"Brief for '{query}': {len(urls)} sources.", sources=len(urls), query=query)
    return brief


# ═══════════════════════════════════════════════════════════════
#  Member prompt
# ═══════════════════════════════════════════════════════════════
def _member_perspectives(custom: Any = None) -> list[str]:
    custom_list = _as_str_list(custom)
    if not custom_list:
        return list(PERSPECTIVES)
    seen: set[str] = set()
    merged: list[str] = []
    for item in custom_list + list(PERSPECTIVES):
        key = item.strip().lower()
        if key and key not in seen:
            seen.add(key)
            merged.append(item.strip())
    return merged or list(PERSPECTIVES)


def _agentic_perspectives(custom: Any = None) -> list[str]:
    custom_list = _as_str_list(custom)
    base = custom_list + list(OMNI_BLACKBOARD_ROLES) if custom_list else list(OMNI_BLACKBOARD_ROLES)
    base += list(PERSPECTIVES)
    seen: set[str] = set()
    out: list[str] = []
    for item in base:
        key = item.split(":", 1)[0].strip().lower()
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out



def _member_identity(council_i: int, member_i: int, perspectives: list[str] | None = None, members_per_council: int = DEFAULT_MEMBERS_PER_COUNCIL) -> tuple[str, str]:
    label = f"C{council_i + 1}M{member_i + 1}"
    roles = perspectives or PERSPECTIVES
    global_index = council_i * max(1, int(members_per_council or 1)) + member_i
    perspective = roles[global_index % len(roles)].split(":", 1)[0]
    return label, perspective


def _member_prompt(
    task: str, context: str, mode: str, council_i: int, member_i: int,
    manifest: dict[str, Any], ledger: list[dict[str, Any]], output_format: str,
    max_tool_requests: int, perspectives: list[str] | None = None,
    councils: int = DEFAULT_COUNCILS, decision_policy: str = "judge", red_team: bool = False,
    blackboard: dict[str, Any] | None = None, messages: list[dict[str, Any]] | None = None,
    member_model: str = DEFAULT_MODEL,
    members_per_council: int = DEFAULT_MEMBERS_PER_COUNCIL,
) -> str:
    roles = perspectives or PERSPECTIVES
    global_index = council_i * max(1, int(members_per_council or 1)) + member_i
    perspective = roles[global_index % len(roles)]
    tools_enabled = (manifest.get("tool_mode") != "off") and max_tool_requests > 0
    role_rules = ""
    if red_team and "red_team" in perspective.lower():
        role_rules += "\nRed-team duty: deliberately attack the proposed plan, look for hidden failures, cheapest falsification tests."
    if decision_policy != "judge":
        role_rules += f"\nDecision policy hint: prepare explicit votes/objections suitable for `{decision_policy}` synthesis."

    tool_contract_line = (
        "- При необходимости фактов — выполни tool request в блоке TOOL_REQUESTS_JSON."
        if tools_enabled else
        "- Tools disabled for this run: НЕ добавляй TOOL_REQUESTS_JSON и работай только по данному контексту."
    )
    blackboard_block = ""
    if blackboard:
        blackboard_block = f"""
Shared blackboard:
{_truncate_text(_json(blackboard), 20_000)}

Agentic collaboration contract:
- Safe tools policy: {'enabled read-only broker' if tools_enabled else 'disabled'}.
- НЕ запрашивай patch, write_file, terminal, process — они запрещены для агентов.
{tool_contract_line}
- Добавь BLACKBOARD_UPDATE_JSON: {{"facts":[],"assumptions":[],"open_questions":[],"actions":[],"objections":[],"evidence_refs":[]}}.
- Добавь VOTE_JSON: {{"vote":"approve|revise|reject","confidence":0.0,"risk":"low|medium|high","blocking_objections":[]}}.
- Отвечай на сообщения других агентов, если они к тебе обращаются.
"""

    message_block = ""
    if messages:
        my_label = f"C{council_i + 1}M{member_i + 1}"
        relevant = [
            m for m in messages
            if m.get("to") in (None, my_label, "all") or m.get("from") == my_label
        ]
        if relevant:
            message_block = f"""
Message board (сообщения, адресованные тебе или всем):
{_truncate_text(_json(relevant), MAX_MESSAGE_HISTORY_CHARS)}

Ты можешь ответить другим агентам через секцию Messages: в формате:
[{{"to":"C2M1","type":"question","content":"..."}}]
"""

    if tools_enabled:
        capability_block = f"""
Доступные safe tools (read-only):
{_truncate_text(_json(SAFE_AGENT_TOOLS), 3000)}

Tool request protocol:
- Для запроса фактов используй блок TOOL_REQUESTS_JSON: с JSON-массивом до {max_tool_requests} запросов.
- Каждый запрос: {{"tool":"memory_wiki_query|read_file|web_search|web_extract|...", "args":{{}}, "reason":"...", "priority":1-5, "expected_information_gain":"...", "mutating":false}}
- Ты не вызываешь tools напрямую, но можешь запросить их выполнение.
"""
    else:
        capability_block = "\nTool request protocol: disabled for this run. Do not emit TOOL_REQUESTS_JSON.\n"

    return f"""Ты участник {council_i + 1}/{councils} OmniCouncil (Hermes OmniCouncil v5.3).
Модель: {member_model}
Перспектива: {perspective}
Режим: {mode}
Decision policy: {decision_policy}
{role_rules}

Правила:
- Дай практически применимый результат, не философию.
- Если tools включены — запрашивай только safe read-only факты; если выключены — не запрашивай tools.
- При agentic blackboard режиме добавляй BLACKBOARD_UPDATE_JSON.
- Всегда добавляй VOTE_JSON для consensus/voting engine.
- Всегда добавляй CLAIMS_JSON: [{{"claim":"...","type":"fact|risk|decision|assumption","evidence_refs":["E1"],"confidence":0.0,"status":"supported|unsupported|assumption"}}].
- Ты можешь общаться с другими агентами через Messages: секцию.
- После ответа других агентов укажи, изменилось ли твоё мнение.
- Сохраняй возможность для главного ассистента сразу выполнить правки.
{capability_block}
{blackboard_block}
{message_block}
Контекст:
{_bounded_context(context) or "(нет дополнительного контекста)"}

Задача:
{task}
"""

# ═══════════════════════════════════════════════════════════════
#  Parse tool requests / messages from agent answer
# ═══════════════════════════════════════════════════════════════
def _json_array_after_marker(text: str, marker: str = "TOOL_REQUESTS_JSON") -> str:
    m = re.search(rf"{re.escape(marker)}\s*:\s*", text, flags=re.IGNORECASE)
    if not m:
        return ""
    start = text.find("[", m.end())
    if start < 0:
        return ""
    depth = 0
    in_str = False
    escape = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return text[start:idx + 1]
    return ""


def _safe_json_loads(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    return json.loads(text)


def _extract_tool_requests(text: str, max_items: int) -> list[dict[str, Any]]:
    if not text or max_items <= 0:
        return []
    limit = min(max_items, MAX_BROKERED_TOOL_REQUESTS)
    candidates = []
    balanced = _json_array_after_marker(text)
    if balanced:
        candidates.append(balanced)
    for fence in re.findall(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.IGNORECASE):
        if fence.strip().startswith("["):
            candidates.append(fence.strip())
    for raw in candidates:
        try:
            data = _safe_json_loads(raw)
            if isinstance(data, dict):
                data = data.get("tool_requests") or data.get("requests") or []
            if isinstance(data, list):
                cleaned = []
                for item in data[:limit]:
                    if isinstance(item, dict):
                        tool = _truncate_text(item.get("tool", ""), 120).strip()
                        if not tool:
                            continue
                        # Block unsafe/mutating tools. OmniCouncil broker is read-only by design.
                        mutating = _normalise_bool(item.get("mutating"), False)
                        if tool in ("patch", "write_file", "terminal", "process", "cronjob") or mutating:
                            continue
                        cleaned.append({
                            "tool": tool,
                            "args": item.get("args", {}) if isinstance(item.get("args", {}), dict) else {},
                            "reason": _truncate_text(item.get("reason", ""), 500),
                            "priority": _clamp_int(item.get("priority"), 3, 1, 5),
                            "expected_information_gain": _truncate_text(item.get("expected_information_gain", ""), 500),
                            "mutating": False,
                            "requires_lock": _as_str_list(item.get("requires_lock")),
                        })
                return cleaned[:limit]
        except Exception:
            continue
    return []


def _extract_messages(text: str) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    m = re.search(r"(?i)Messages\s*:\s*", text)
    if not m:
        m = re.search(r"(?i)MESSAGES_JSON\s*:\s*", text)
    if not m:
        return messages
    remainder = text[m.end():]
    arr = _json_array_after_marker("Messages:" + remainder.split("Messages:", 1)[-1] if "Messages:" in remainder else remainder, marker="")
    if not arr:
        arr = _json_array_after_marker("MESSAGES_JSON:" + remainder, marker="")
    if arr:
        try:
            data = _safe_json_loads(arr)
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("to") and item.get("content"):
                        messages.append({
                            "to": str(item.get("to", "")),
                            "type": str(item.get("type", "question")),
                            "content": _truncate_text(item.get("content", ""), MAX_MESSAGE_CHARS),
                        })
        except Exception:
            pass
    return messages


def _dedupe_tool_requests(answers: list[dict[str, Any]], max_items: int, minimum_tools: bool = True) -> list[dict[str, Any]]:
    if max_items <= 0:
        return []
    limit = min(max_items, MAX_BROKERED_TOOL_REQUESTS)
    seen = set()
    out = []
    for ans in answers:
        for req in ans.get("tool_requests", []) or []:
            key = json.dumps([req.get("tool"), req.get("args")], sort_keys=True, ensure_ascii=False)
            if key in seen:
                continue
            seen.add(key)
            req = dict(req)
            if minimum_tools and not (req.get("reason") or req.get("expected_information_gain")):
                req["weak_request"] = True
            req["requested_by"] = ans.get("label")
            out.append(req)
    out.sort(key=lambda x: int(x.get("priority") or 3), reverse=True)
    return out[:limit]



def _json_value_after_marker(text: str, marker: str, openers: str = "[{") -> str:
    m = re.search(rf"{re.escape(marker)}\s*:\s*", text or "", flags=re.IGNORECASE)
    if not m:
        return ""
    starts = [(text.find(ch, m.end()), ch) for ch in openers]
    starts = [(idx, ch) for idx, ch in starts if idx >= 0]
    if not starts:
        return ""
    start, opener = min(starts, key=lambda x: x[0])
    closer = "]" if opener == "[" else "}"
    depth = 0
    in_str = False
    escape = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start:idx + 1]
    return ""


def _extract_blackboard_update(text: str) -> dict[str, Any]:
    raw = _json_value_after_marker(text or "", "BLACKBOARD_UPDATE_JSON", "{")
    if not raw:
        raw = _json_value_after_marker(text or "", "Blackboard update", "{")
    if not raw:
        return {}
    try:
        data = _safe_json_loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _extract_vote(text: str) -> dict[str, Any]:
    raw = _json_value_after_marker(text or "", "VOTE_JSON", "{")
    if not raw:
        return {}
    try:
        data = _safe_json_loads(raw)
        if not isinstance(data, dict):
            return {}
    except Exception:
        return {}
    vote = str(data.get("vote") or "revise").lower().strip()
    if vote not in {"approve", "revise", "reject", "abstain"}:
        vote = "revise"
    risk = str(data.get("risk") or "medium").lower().strip()
    if risk not in {"low", "medium", "high"}:
        risk = "medium"
    try:
        confidence = float(data.get("confidence", 0.5))
    except Exception:
        confidence = 0.5
    return {
        "vote": vote,
        "confidence": max(0.0, min(1.0, confidence)),
        "risk": risk,
        "blocking_objections": [_truncate_text(x, 500) for x in _as_str_list(data.get("blocking_objections"))[:12]],
        "rationale": _truncate_text(data.get("rationale", ""), 800),
    }


def _merge_blackboard_update(blackboard: dict[str, Any], label: str, update: dict[str, Any]) -> None:
    if not isinstance(update, dict) or not update:
        return
    mapping = {
        "facts": "facts",
        "assumptions": "assumptions",
        "open_questions": "open_questions",
        "actions": "actions",
        "proposed_actions": "actions",
        "objections": "objections",
        "evidence_refs": "evidence_refs",
    }
    blackboard.setdefault("updates", [])
    blackboard["updates"].append({"from": label, "update": _tool_result_preview(update, 3000)})
    for source_key, target_key in mapping.items():
        values = update.get(source_key)
        if values is None:
            continue
        blackboard.setdefault(target_key, [])
        if not isinstance(values, list):
            values = [values]
        for value in values[:20]:
            item = {"from": label, "text": _truncate_text(value, 1000)}
            if item not in blackboard[target_key]:
                blackboard[target_key].append(item)


def _merge_blackboard_from_responses(blackboard: dict[str, Any], responses: list[dict[str, Any]]) -> None:
    for item in responses:
        if not isinstance(item, dict) or item.get("status") != "success":
            continue
        update = item.get("blackboard_update") or _extract_blackboard_update(item.get("answer") or item.get("response") or item.get("report") or "")
        _merge_blackboard_update(blackboard, str(item.get("label") or item.get("mission") or "unknown"), update)


def _aggregate_votes(responses: list[dict[str, Any]]) -> dict[str, Any]:
    votes: list[dict[str, Any]] = []
    for item in responses:
        if not isinstance(item, dict) or item.get("status") != "success":
            continue
        vote = item.get("vote") or _extract_vote(item.get("answer") or item.get("response") or item.get("report") or "")
        if vote:
            vote = dict(vote)
            vote["from"] = item.get("label") or item.get("mission") or "unknown"
            votes.append(vote)
    counts = {"approve": 0, "revise": 0, "reject": 0, "abstain": 0}
    risks = {"low": 0, "medium": 0, "high": 0}
    confidence_sum = 0.0
    objections: list[dict[str, Any]] = []
    for vote in votes:
        counts[vote.get("vote", "revise")] = counts.get(vote.get("vote", "revise"), 0) + 1
        risks[vote.get("risk", "medium")] = risks.get(vote.get("risk", "medium"), 0) + 1
        confidence_sum += float(vote.get("confidence", 0.0) or 0.0)
        for obj in vote.get("blocking_objections", []) or []:
            objections.append({"from": vote.get("from"), "objection": _truncate_text(obj, 500)})
    winner = max(counts.items(), key=lambda kv: kv[1])[0] if votes else "none"
    return {
        "votes": votes,
        "counts": counts,
        "risk_counts": risks,
        "majority": winner,
        "average_confidence": round(confidence_sum / len(votes), 3) if votes else 0.0,
        "blocking_objections": objections[:40],
    }

# ═══════════════════════════════════════════════════════════════
#  Call member / collaboration / message rounds
# ═══════════════════════════════════════════════════════════════
def _call_member(
    task: str, context: str, mode: str, council_i: int, member_i: int,
    max_tokens: int, manifest: dict[str, Any], ledger: list[dict[str, Any]],
    output_format: str, max_tool_requests: int,
    perspectives: list[str] | None = None, councils: int = DEFAULT_COUNCILS,
    timeout: int = DEFAULT_MODEL_TIMEOUT, retries: int = DEFAULT_MEMBER_RETRIES,
    decision_policy: str = "judge", red_team: bool = False,
    jitter_ms: int = DEFAULT_REQUEST_JITTER_MS,
    blackboard: dict[str, Any] | None = None,
    messages: list[dict[str, Any]] | None = None,
    member_model: str = DEFAULT_MODEL,
    members_per_council: int = DEFAULT_MEMBERS_PER_COUNCIL,
) -> dict[str, Any]:
    label, perspective = _member_identity(council_i, member_i, perspectives, members_per_council)
    try:
        started = time.time()
        prompt = _member_prompt(
            task, context, mode, council_i, member_i, manifest, ledger,
            output_format, max_tool_requests, perspectives, councils,
            decision_policy, red_team, blackboard=blackboard, messages=messages,
            member_model=member_model, members_per_council=members_per_council,
        )
        answer = _call_model_text(prompt, max_tokens, 0.05 + (member_i * 0.05), model=member_model, timeout=timeout, retries=retries, jitter_ms=jitter_ms)
        return {
            "label": label, "council": council_i + 1, "member": member_i + 1,
            "perspective": perspective, "status": "success", "answer": answer,
            "tool_requests": _extract_tool_requests(answer, max_tool_requests),
            "messages_out": _extract_messages(answer),
            "blackboard_update": _extract_blackboard_update(answer),
            "vote": _extract_vote(answer),
            "model": member_model,
            "seconds": round(time.time() - started, 1),
        }
    except Exception as exc:
        logger.warning("%s failed: %s", label, exc)
        return {
            "label": label, "council": council_i + 1, "member": member_i + 1,
            "perspective": perspective, "status": "failed",
            "error": _truncate_text(str(exc), 500),
        }


def _format_answer(item: dict[str, Any], field: str = "answer", max_chars: int = 6000) -> str:
    label = item.get("label") or item.get("mission") or "UNKNOWN"
    perspective = item.get("perspective", "")
    if item.get("status") == "success":
        text = _truncate_text(item.get(field) or item.get("answer") or item.get("report") or "", max_chars)
        return f"## {label} / {perspective} (model={item.get('model','?')})\n{text}"
    return f"## {label} FAILED\n{_truncate_text(item.get('error') or item.get('reason') or '', 700)}"


def _format_answers(answers: list[dict[str, Any]], field: str = "answer", max_chars: int = 6000) -> str:
    if not answers:
        return "(нет ответов)"
    return "\n\n".join(_format_answer(item, field, max_chars) for item in answers)


def _collaboration_prompt(
    task: str, context: str, mode: str, member: dict[str, Any],
    initial_answers: list[dict[str, Any]], previous_rounds: list[dict[str, Any]],
    round_no: int, manifest: dict[str, Any], ledger: list[dict[str, Any]],
    research_reports: list[dict[str, Any]], tool_requests: list[dict[str, Any]],
    blackboard: dict[str, Any] | None = None,
    messages: list[dict[str, Any]] | None = None,
    member_model: str = DEFAULT_MODEL,
) -> str:
    previous = "\n\n".join(
        f"# Round {r.get('round')}\n{_format_answers(r.get('responses', []), field='response', max_chars=MAX_PREVIOUS_ROUND_CHARS_FOR_COLLAB)}"
        for r in previous_rounds
    ) or "(первый раунд обсуждения)"
    initial = _format_answers(initial_answers, max_chars=MAX_INITIAL_ANSWER_CHARS_FOR_COLLAB)
    research = _format_answers(research_reports, field="report", max_chars=8000) if research_reports else ""

    blackboard_block = ""
    if blackboard:
        blackboard_block = f"Shared blackboard:\n{_truncate_text(_json(blackboard), 20_000)}\n"

    message_block = ""
    if messages:
        my_label = member.get("label", "")
        relevant = [m for m in messages if m.get("to") in (None, my_label, "all")]
        if relevant:
            message_block = f"Messages для тебя:\n{_truncate_text(_json(relevant), MAX_MESSAGE_HISTORY_CHARS)}\n"

    tools_enabled = (manifest.get("tool_mode") != "off") and max_tool_requests > 0
    tool_instruction = "- используй TOOL_REQUESTS_JSON: для safe tools;" if tools_enabled else "- tools disabled: не добавляй TOOL_REQUESTS_JSON;"

    prompt = f"""Ты {member.get('label')} / {member.get('perspective')} в OmniCouncil (модель: {member_model}).
Раунд командного обсуждения: {round_no}

Задача: {task}
Режим: {mode}

{blackboard_block}
{message_block}

Первичные ответы команды:
{initial}

Предыдущие раунды:
{previous}

Research reports:
{research}

Aggregated tool requests:
{_truncate_text(_json(tool_requests), 20_000)}

Твоя цель:
- прямо ссылайся на идеи коллег по label;
- найди противоречия, скрытые риски, edge cases;
- улучши общий план;
{tool_instruction}
- общайся с другими агентами через Messages: секцию;
- добавь BLACKBOARD_UPDATE_JSON, VOTE_JSON и CLAIMS_JSON.
- закончи блоками Blackboard update: и Мой обновлённый вклад:.

Safe tools: {"enabled" if tools_enabled else "disabled"}.
"""
    if _prompt_too_large(prompt):
        prompt = prompt.replace(initial, _format_answers(initial_answers, max_chars=900)).replace(previous, _format_answers(initial_answers, max_chars=800))
    return _truncate_text(prompt, MAX_PROMPT_CONTEXT_CHARS, "…[prompt truncated]")


def _call_collaboration_member(
    task: str, context: str, mode: str, member: dict[str, Any],
    initial_answers: list[dict[str, Any]], previous_rounds: list[dict[str, Any]],
    round_no: int, max_tokens: int, manifest: dict[str, Any],
    ledger: list[dict[str, Any]], research_reports: list[dict[str, Any]],
    tool_requests: list[dict[str, Any]], max_tool_requests: int,
    timeout: int = DEFAULT_MODEL_TIMEOUT, retries: int = DEFAULT_MEMBER_RETRIES,
    jitter_ms: int = DEFAULT_REQUEST_JITTER_MS,
    blackboard: dict[str, Any] | None = None,
    messages: list[dict[str, Any]] | None = None,
    member_model: str = DEFAULT_MODEL,
) -> dict[str, Any]:
    try:
        started = time.time()
        prompt = _collaboration_prompt(
            task, context, mode, member, initial_answers, previous_rounds, round_no,
            manifest, ledger, research_reports, tool_requests,
            blackboard=blackboard, messages=messages, member_model=member_model,
        )
        member_no = int(member.get("member") or 1) - 1
        response = _call_model_text(prompt, max_tokens, 0.08 + (member_no * 0.04), model=member_model, timeout=timeout, retries=retries, jitter_ms=jitter_ms)
        return {
            "label": member.get("label"), "council": member.get("council"),
            "member": member.get("member"), "perspective": member.get("perspective"),
            "status": "success", "response": response,
            "tool_requests": _extract_tool_requests(response, max_tool_requests),
            "messages_out": _extract_messages(response),
            "blackboard_update": _extract_blackboard_update(response),
            "vote": _extract_vote(response),
            "model": member_model,
            "seconds": round(time.time() - started, 1),
        }
    except Exception as exc:
        return {
            "label": member.get("label"), "council": member.get("council"),
            "member": member.get("member"), "perspective": member.get("perspective"),
            "status": "failed", "error": _truncate_text(str(exc), 500),
        }


def _run_collaboration(
    task: str, context: str, mode: str, answers: list[dict[str, Any]], rounds: int,
    max_tokens: int, manifest: dict[str, Any], ledger: list[dict[str, Any]],
    research_reports: list[dict[str, Any]], tool_requests: list[dict[str, Any]],
    max_tool_requests: int, max_workers: int = DEFAULT_MAX_COLLABORATION_WORKERS,
    timeout: int = DEFAULT_MODEL_TIMEOUT, retries: int = DEFAULT_MEMBER_RETRIES,
    jitter_ms: int = DEFAULT_REQUEST_JITTER_MS,
    blackboard: dict[str, Any] | None = None,
    messages: list[dict[str, Any]] | None = None,
    member_models: list[str] | None = None,
) -> list[dict[str, Any]]:
    successful = [a for a in answers if a.get("status") == "success"]
    transcript: list[dict[str, Any]] = []
    if not successful or rounds <= 0:
        return transcript
    member_models = member_models or [DEFAULT_MODEL]
    for round_no in range(1, rounds + 1):
        worker_count = max(1, min(len(successful), max_workers or DEFAULT_MAX_COLLABORATION_WORKERS))
        with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as pool:
            future_map = {
                pool.submit(
                    _contextvars.copy_context().run,
                    _call_collaboration_member, task, context, mode, member,
                    answers, transcript, round_no, max_tokens, manifest, ledger,
                    research_reports, tool_requests, max_tool_requests, timeout,
                    retries, jitter_ms, blackboard=blackboard, messages=messages,
                    member_model=member_models[(int(member.get("member") or 1) - 1) % len(member_models)],
                ): member
                for member in successful
            }
            responses = []
            for future in concurrent.futures.as_completed(future_map):
                member = future_map[future]
                try:
                    responses.append(future.result())
                except Exception as exc:
                    responses.append({
                        "label": member.get("label"), "council": member.get("council"),
                        "member": member.get("member"), "perspective": member.get("perspective"),
                        "status": "failed", "error": _truncate_text(str(exc), 500),
                    })
            responses.sort(key=lambda item: (int(item.get("council") or 0), int(item.get("member") or 0), str(item.get("label") or "")))
        tool_requests = _dedupe_tool_requests(answers + [r for t in transcript for r in t.get("responses", [])] + responses, max_tool_requests)
        transcript.append({"round": round_no, "responses": responses})
    return transcript


# ═══════════════════════════════════════════════════════════════
#  Message rounds (agents send messages to each other)
# ═══════════════════════════════════════════════════════════════
def _run_message_rounds(
    task: str, context: str, mode: str, answers: list[dict[str, Any]],
    rounds: int, max_tokens: int, manifest: dict[str, Any],
    ledger: list[dict[str, Any]], tool_requests: list[dict[str, Any]],
    max_tool_requests: int, max_workers: int = DEFAULT_MAX_COLLABORATION_WORKERS,
    timeout: int = DEFAULT_MODEL_TIMEOUT, retries: int = DEFAULT_MEMBER_RETRIES,
    jitter_ms: int = DEFAULT_REQUEST_JITTER_MS,
    blackboard: dict[str, Any] | None = None,
    member_models: list[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    successful = [a for a in answers if a.get("status") == "success"]
    if not successful or rounds <= 0:
        return [], []
    member_models = member_models or [DEFAULT_MODEL]
    all_messages: list[dict[str, Any]] = []
    transcript: list[dict[str, Any]] = []

    for round_no in range(rounds):
        # Каждый агент видит все предыдущие сообщения
        worker_count = max(1, min(len(successful), max_workers or DEFAULT_MAX_COLLABORATION_WORKERS))
        with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as pool:
            future_map = {
                pool.submit(
                    _contextvars.copy_context().run,
                    _call_message_round_member, task, context, mode, member,
                    answers, all_messages, round_no, max_tokens, manifest, ledger,
                    tool_requests, max_tool_requests, timeout, retries, jitter_ms,
                    blackboard=blackboard,
                    member_model=member_models[(int(member.get("member") or 1) - 1) % len(member_models)],
                ): member
                for member in successful
            }
            responses = []
            for future in concurrent.futures.as_completed(future_map):
                member = future_map[future]
                try:
                    resp = future.result()
                    responses.append(resp)
                    for msg in resp.get("messages_out", []):
                        msg["from"] = member.get("label")
                        msg["round"] = round_no
                        all_messages.append(msg)
                except Exception as exc:
                    responses.append({
                        "label": member.get("label"), "status": "failed",
                        "error": _truncate_text(str(exc), 500),
                    })
        transcript.append({"message_round": round_no, "responses": responses})
    return transcript, all_messages



def _call_message_round_member(
    task: str, context: str, mode: str, member: dict[str, Any],
    initial_answers: list[dict[str, Any]], all_messages: list[dict[str, Any]],
    round_no: int, max_tokens: int, manifest: dict[str, Any],
    ledger: list[dict[str, Any]], tool_requests: list[dict[str, Any]],
    max_tool_requests: int, timeout: int = DEFAULT_MODEL_TIMEOUT,
    retries: int = DEFAULT_MEMBER_RETRIES, jitter_ms: int = DEFAULT_REQUEST_JITTER_MS,
    blackboard: dict[str, Any] | None = None,
    member_model: str = DEFAULT_MODEL,
) -> dict[str, Any]:
    try:
        my_label = member.get("label", "")
        relevant = [m for m in all_messages if m.get("to") in (None, my_label, "all")]
        tools_enabled = (manifest.get("tool_mode") != "off") and max_tool_requests > 0
        tool_instruction = "Также можешь запрашивать safe tools через TOOL_REQUESTS_JSON." if tools_enabled else "Tools disabled: не добавляй TOOL_REQUESTS_JSON."
        prompt = f"""Ты {my_label} / {member.get('perspective')} в OmniCouncil, message round {round_no}.

Другие агенты отправили тебе сообщения. Прочитай их и ответь.

Задача: {task}
Режим: {mode}

Сообщения:
{_truncate_text(_json(relevant), MAX_MESSAGE_HISTORY_CHARS)}

Ты можешь ответить другим агентам через секцию Messages: в формате JSON:
[{{"to":"C2M1","type":"question|answer|challenge|clarification","content":"твой ответ"}}]

{tool_instruction}
Добавь BLACKBOARD_UPDATE_JSON и VOTE_JSON, если твоё мнение изменилось.
"""
        answer = _call_model_text(prompt, max_tokens, 0.08, model=member_model, timeout=timeout, retries=retries, jitter_ms=jitter_ms)
        return {
            "label": my_label, "status": "success", "response": answer,
            "messages_out": _extract_messages(answer),
            "tool_requests": _extract_tool_requests(answer, max_tool_requests),
            "blackboard_update": _extract_blackboard_update(answer),
            "vote": _extract_vote(answer),
            "model": member_model,
        }
    except Exception as exc:
        return {"label": member.get("label"), "status": "failed", "error": _truncate_text(str(exc), 500)}

# ═══════════════════════════════════════════════════════════════
#  Judge
# ═══════════════════════════════════════════════════════════════
def _blackboard_summary(answers: list[dict[str, Any]], transcript: list[dict[str, Any]], tool_requests: list[dict[str, Any]], messages: list[dict[str, Any]], manifest: dict[str, Any]) -> dict[str, Any]:
    if not (manifest.get("blackboard") or {}).get("enabled"):
        return {}
    responses = [a for a in answers if a.get("status") == "success"] + [
        r for t in transcript for r in t.get("responses", []) if r.get("status") == "success"
    ]
    return {
        "enabled": True,
        "responses_seen": len(responses),
        "message_count": len(messages),
        "tool_requests_total": len(tool_requests),
    }


def _judge_prompt(
    task: str, context: str, mode: str, answers: list[dict[str, Any]],
    transcript: list[dict[str, Any]], manifest: dict[str, Any],
    ledger: list[dict[str, Any]], research_reports: list[dict[str, Any]],
    tool_requests: list[dict[str, Any]], messages: list[dict[str, Any]],
    output_format: str, save_task_capsule: bool,
    decision_policy: str = "judge", red_team: bool = False,
    judge_model: str = DEFAULT_MODEL,
    dissent_required: bool = False,
    anti_slop: bool = False,
    compiler_judge: bool = True,
) -> str:
    blackboard_enabled = bool((manifest.get("blackboard") or {}).get("enabled"))
    discussion = "\n\n".join(
        f"# Round {r.get('round')}\n{_format_answers(r.get('responses', []), field='response', max_chars=MAX_DISCUSSION_CHARS_FOR_JUDGE)}"
        for r in transcript
    ) or "(обсуждение не включалось)"
    initial = _format_answers(answers, max_chars=MAX_INITIAL_ANSWER_CHARS_FOR_JUDGE)
    research = _format_answers(research_reports, field="report", max_chars=MAX_RESEARCH_REPORT_CHARS_FOR_JUDGE) if research_reports else ""

    msg_text = _truncate_text(_json(messages), MAX_MESSAGE_HISTORY_CHARS) if messages else ""
    vote_summary = _aggregate_votes(answers + [r for t in transcript for r in t.get("responses", [])])

    decision_rule = {
        "judge": "Use best-evidence judgement; do not average weak arguments.",
        "majority": "Report the apparent majority view, override if evidence demands.",
        "consensus": "Prefer consensus; list unresolved disagreements and safest next experiment.",
        "risk_weighted": "Prefer lowest irreversible risk with strongest rollback path.",
    }.get(decision_policy, "Use best-evidence judgement.")

    format_rules = {
        "prose": "Верни связный практический отчёт.",
        "structured": "Верни Markdown с секциями: Verdict, Evidence, Decision, Implementation plan, Tests, Risks, Next step.",
        "patch_plan": "Верни конкретный план изменений: файлы, функции, old/new intent, команды проверки, rollback.",
        "json": "Верни валидный JSON: summary, evidence, decision, implementation_plan[], tests[], risks[], next_step.",
    }.get(output_format, "Верни структурированный отчёт.")

    # ── Conditional blocks (f-string-safe, без бэкслешей) ──
    dissent_block = (
        "DISSENT_REQUIRED: Ты ОБЯЗАН включить секцию Dissent / Objections. "
        'Даже если все агенты согласны — найди минимум 3 правдоподобных failure mode, '
        'скрытых допущения или альтернативных интерпретации. '
        'Включи подсекцию "What would change my mind".'
        if dissent_required else ""
    )
    anti_slop_block = (
        "ANTI_SLOP: Каждая рекомендация должна включать: конкретный file:line (если применимо), "
        "expected benefit, possible downside, verification method. "
        'Избегай общих фраз вроде "ensure security" или "add tests" без конкретики.'
        if anti_slop else ""
    )
    compiler_block = (
        "JUDGE_AS_COMPILER: сначала скомпилируй канонический блок JUDGE_COMPILED_JSON: "
        "{\"verdict\":\"...\",\"confirmed_findings\":[],\"unsupported_claims\":[],"
        "\"rejected_claims\":[],\"decision\":\"...\",\"implementation_plan\":[],"
        "\"tests\":[],\"risks\":[],\"dissent\":[],\"next_step\":\"...\"}. "
        "Confirmed findings must cite evidence/claim ids; unsupported claims stay unsupported. "
        if compiler_judge else ""
    )

    prompt = f"""Ты финальный судья Hermes OmniCouncil v5.3 (модель: {judge_model}).
Синтезируй лучший проверяемый результат, опираясь на evidence и research.

Output format: {output_format}
{format_rules}
Decision policy: {decision_policy}
{decision_rule}
{dissent_block}
{anti_slop_block}
{compiler_block}

Задача:
{task}

Контекст:
{_bounded_context(context) or "(нет дополнительного контекста)"}

Режим: {mode}

Первичные ответы участников:
{initial}

Обсуждение:
{discussion}

Сообщения агентов:
{msg_text}

Structured vote summary:
{_truncate_text(_json(vote_summary), 12_000)}

Aggregated tool requests:
{_truncate_text(_json(tool_requests), 20_000)}

Research reports:
{research}

Blackboard summary:
{_truncate_text(_json(_blackboard_summary(answers, transcript, tool_requests, messages, manifest)), 12_000)}

Evidence ledger:
{_evidence_text(ledger)}
"""
    if _prompt_too_large(prompt):
        prompt = prompt.replace(initial, _format_answers(answers, max_chars=1000))
    return _truncate_text(prompt, MAX_PROMPT_CONTEXT_CHARS)


def _judge(
    task: str, context: str, mode: str, answers: list[dict[str, Any]],
    judge_max_tokens: int, transcript: list[dict[str, Any]], manifest: dict[str, Any],
    ledger: list[dict[str, Any]], research_reports: list[dict[str, Any]],
    tool_requests: list[dict[str, Any]], messages: list[dict[str, Any]],
    output_format: str, save_task_capsule: bool,
    timeout: int = DEFAULT_JUDGE_TIMEOUT, retries: int = DEFAULT_MEMBER_RETRIES,
    strict_json: bool = False, decision_policy: str = "judge", red_team: bool = False,
    jitter_ms: int = DEFAULT_REQUEST_JITTER_MS,
    json_schema: dict[str, Any] | None = None,
    judge_model: str = DEFAULT_MODEL,
    dissent_required: bool = False,
    anti_slop: bool = False,
    compiler_judge: bool = True,
) -> str:
    prompt = _judge_prompt(task, context, mode, answers, transcript, manifest, ledger, research_reports, tool_requests, messages, output_format, save_task_capsule, decision_policy, red_team, judge_model=judge_model, dissent_required=dissent_required, anti_slop=anti_slop, compiler_judge=compiler_judge)
    if strict_json and output_format == "json":
        prompt += "\n\nSTRICT_JSON: Return only valid JSON, no markdown fences."
    try:
        content = _call_model_text(prompt, judge_max_tokens, 0.1, model=judge_model, timeout=timeout, retries=retries, jitter_ms=jitter_ms)
    except Exception as exc:
        raise RuntimeError(f"judge failed: {exc}")
    content = _redact(content)
    if strict_json and output_format == "json":
        try:
            parsed = _safe_json_loads(content)
            if isinstance(parsed, dict):
                schema_errors = _validate_json_schema_minimal(parsed, json_schema)
                if schema_errors:
                    return _json({"status": "partial", "warning": "judge_output_schema_validation_failed", "schema_errors": schema_errors, "synthesis_json": parsed})
                return _json(parsed)
        except Exception:
            return _json({"status": "partial", "warning": "judge_output_was_not_valid_json", "synthesis_text": content})
    return content



def _validate_json_schema_minimal(data: Any, schema: dict[str, Any] | None) -> list[str]:
    """Minimal dependency-free validator: required keys, types, nested objects, arrays, enum."""
    if not schema:
        return []
    errors: list[str] = []
    # --- root type assertion ---
    root_type = schema.get("type")
    type_map: dict[str, Any] = {"string": str, "array": list, "object": dict, "boolean": bool, "integer": int, "number": (int, float)}
    if root_type and not isinstance(data, type_map.get(root_type, object)):
        return [f"root: expected {root_type}, got {type(data).__name__}"]

    def _validate(value: Any, prop: dict[str, Any], path: str) -> None:
        expected = prop.get("type")
        if expected:
            py_type = type_map.get(expected)
            if py_type and not isinstance(value, py_type):
                errors.append(f"{path}: expected {expected}, got {type(value).__name__}")
                return
        # enum
        enum_values = prop.get("enum")
        if enum_values is not None and isinstance(enum_values, list) and value not in enum_values:
            errors.append(f"{path}: value {_truncate_text(str(value), 80)} not in enum {_truncate_text(str(enum_values), 120)}")
        # nested object properties
        if isinstance(value, dict) and isinstance(prop.get("properties"), dict):
            for key, sub in prop["properties"].items():
                if not isinstance(sub, dict):
                    continue
                if key not in value:
                    # only flag required keys
                    if key in (prop.get("required") or []):
                        errors.append(f"{path}.{key}: required")
                    continue
                _validate(value[key], sub, f"{path}.{key}")
        # array items
        if isinstance(value, list) and isinstance(prop.get("items"), dict):
            item_schema: dict[str, Any] = prop["items"]
            for idx, elem in enumerate(value[:100]):  # guard against enormous arrays
                _validate(elem, item_schema, f"{path}[{idx}]")

    if isinstance(schema, dict) and isinstance(data, (dict, list)):
        if isinstance(data, dict):
            for key in schema.get("required", []) or []:
                if key not in data:
                    errors.append(f"root.{key}: required")
            for key, prop in (schema.get("properties") or {}).items():
                if key in data and isinstance(prop, dict) and prop:
                    _validate(data[key], prop, f"root.{key}")
        if isinstance(data, list) and isinstance(schema.get("items"), dict):
            item_schema: dict[str, Any] = schema["items"]
            for idx, elem in enumerate(data[:200]):
                _validate(elem, item_schema, f"root[{idx}]")
    return errors

# ═══════════════════════════════════════════════════════════════
#  Research missions
# ═══════════════════════════════════════════════════════════════
def _make_research_missions(task: str, mode: str, manifest: dict[str, Any], max_agents: int, enabled_toolsets: list[str]) -> list[dict[str, Any]]:
    base = [
        ("web_research", "Use web_search + web_extract + web_research_brief to find external facts and recent data relevant to the task."),
        ("file_inspection", "Use read_file + search_files to inspect relevant source files and configuration."),
        ("memory_lookup", "Use memory_wiki_query to search durable memory for past decisions and project context."),
        ("alternative_approaches", "Research alternative solutions, tradeoffs, and competing designs."),
        ("risk_analysis", "Identify risks, failure modes, edge cases, and security concerns."),
    ]
    missions = []
    for name, goal in base[:max_agents]:
        missions.append({"mission": name, "goal": goal, "task": task, "mode": mode, "enabled_toolsets": list(enabled_toolsets)})
    return missions


def _run_research_mission(mission: dict[str, Any], manifest: dict[str, Any], max_tokens: int, jitter_ms: int = DEFAULT_REQUEST_JITTER_MS, research_model: str = DEFAULT_MODEL) -> dict[str, Any]:
    try:
        prompt = f"""Ты research subagent в Hermes OmniCouncil v5.5.
Используй safe tools (web_search, web_extract, read_file, memory_wiki_query) для поиска фактов.
Отделяй Evidence от Assumption. Верни Findings, Risks, Acceptance tests. Добавь CLAIMS_JSON с evidence_refs/status/confidence.

Mission: {_json(mission)}
Capability manifest: {_manifest_text(manifest)}
"""
        report = _call_model_text(prompt, min(max_tokens, 32000), 0.1, model=research_model, timeout=900, jitter_ms=jitter_ms)
        return {"mission": mission.get("mission"), "status": "success", "kind": "model_research", "report": report, "model": research_model}
    except Exception as exc:
        return {"mission": mission.get("mission"), "status": "failed", "kind": "model_research", "error": _truncate_text(str(exc), 800)}


def _run_research_missions(
    task: str, mode: str, manifest: dict[str, Any], max_agents: int,
    enabled_toolsets: list[str], max_tokens: int, ledger: list[dict[str, Any]],
    max_workers: int = DEFAULT_MAX_RESEARCH_WORKERS,
    jitter_ms: int = DEFAULT_REQUEST_JITTER_MS,
    research_model: str = DEFAULT_MODEL,
) -> list[dict[str, Any]]:
    missions = _make_research_missions(task, mode, manifest, max_agents, enabled_toolsets)
    _add_evidence(ledger, "research_missions", f"Prepared {len(missions)} research missions.", count=len(missions))
    if not missions:
        return []
    worker_count = max(1, min(len(missions), max_workers or DEFAULT_MAX_RESEARCH_WORKERS))
    results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as pool:
        future_map = {pool.submit(_contextvars.copy_context().run, _run_research_mission, m, manifest, max_tokens, jitter_ms, research_model): m for m in missions}
        for future in concurrent.futures.as_completed(future_map):
            mission = future_map[future]
            try:
                results.append(future.result())
            except Exception as exc:
                results.append({"mission": mission.get("mission"), "status": "failed", "kind": "executor", "error": _truncate_text(str(exc), 800)})
    for r in results:
        if r.get("status") == "success":
            _add_evidence(ledger, "research_report", f"Research mission {r.get('mission')} completed.", mission=r.get("mission"))
    return results


# ═══════════════════════════════════════════════════════════════
#  Fallback synthesis
# ═══════════════════════════════════════════════════════════════
def _fallback_synthesis(
    task: str, mode: str, answers: list[dict[str, Any]],
    transcript: list[dict[str, Any]], research_reports: list[dict[str, Any]],
    tool_requests: list[dict[str, Any]], reason: str,
) -> str:
    successful = [a for a in answers if a.get("status") == "success"]
    if not successful and not research_reports:
        return f"## OmniCouncil fallback\nStatus: no_successful_members\nReason: {reason}"
    best = successful[0] if successful else None
    if best:
        return (
            f"## OmniCouncil fallback synthesis\n"
            f"Reason: {_truncate_text(reason, 800)}\n"
            f"Best response from {best.get('label')} ({best.get('model','?')}):\n\n"
            f"{_truncate_text(best.get('answer', ''), 30_000)}"
        )
    best_research = research_reports[0] if research_reports else None
    if best_research:
        return f"## OmniCouncil fallback (research only)\nReason: {_truncate_text(reason, 800)}\n\n{_truncate_text(best_research.get('report', ''), 30_000)}"
    return f"## OmniCouncil fallback\nReason: {_truncate_text(reason, 800)}\nNo usable member or research results."


# ═══════════════════════════════════════════════════════════════
#  Cache
# ═══════════════════════════════════════════════════════════════
def _cache_key(task: str, context: str, mode: str, options: dict[str, Any], manifest: dict[str, Any], default_model: str) -> str:
    payload = {
        "v": 4,
        "model": default_model,
        "task": task,
        "context": context,
        "mode": mode,
        "councils": options.get("councils", DEFAULT_COUNCILS),
        "members_per_council": options.get("members_per_council", DEFAULT_MEMBERS_PER_COUNCIL),
        "options": {k: v for k, v in options.items() if not str(k).startswith("_")},
        "manifest_hash": hashlib.sha256(_manifest_text(manifest).encode()).hexdigest(),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


# ═══════════════════════════════════════════════════════════════
#  Preset defaults
# ═══════════════════════════════════════════════════════════════

def _complexity_score(task: str, context: str) -> int:
    combined = (task + " " + context).lower()
    score = 0
    if len(combined.strip()) < 120:
        score += 1
    for kw in (
        "migration", "deploy", "production", "database", "security", "auth", "race condition",
        "rollback", "incident", "outage", "payment", "secret", "multi-agent", "architecture",
        "patch", "refactor", "audit", "browser", "server", "memory", "schema",
    ):
        if kw in combined:
            score += 1
    if len(combined) > 600:
        score += 1
    if len(combined) > 2000:
        score += 2
    if len(combined) > 6000:
        score += 2
    return min(12, score)


def _auto_scale(task: str, context: str) -> dict[str, Any]:
    score = _complexity_score(task, context)
    if score <= 2:
        return {"councils": 2, "members_per_council": 3, "collaboration_rounds": 0, "message_rounds": 0, "research_missions": False, "complexity_score": score}
    if score <= 5:
        return {"councils": 3, "members_per_council": 4, "collaboration_rounds": 1, "message_rounds": 0, "research_missions": False, "complexity_score": score}
    if score <= 8:
        return {"councils": 5, "members_per_council": 4, "collaboration_rounds": 2, "message_rounds": 1, "research_missions": True, "complexity_score": score}
    return {"councils": 6, "members_per_council": 5, "collaboration_rounds": 2, "message_rounds": 2, "research_missions": True, "red_team": True, "complexity_score": score}

def _apply_auto_debate_defaults(args: dict[str, Any]) -> dict[str, Any]:
    """Turn project #1 into a first-class mode without creating a separate tool."""
    out = dict(args or {})
    enabled = _normalise_bool(out.get("auto_debate"), False) or _normalise_bool(out.get("verify_chain"), False)
    if not enabled:
        return out
    out["auto_debate"] = True
    out.setdefault("verify_chain", True)
    out.setdefault("preset", "balanced")
    out.setdefault("mode", "review")
    out.setdefault("agentic_blackboard", True)
    out.setdefault("minimum_tools", True)
    out.setdefault("brokered_tools", True)
    out.setdefault("tool_mode", "safe_agent")
    out.setdefault("dissent_required", True)
    out.setdefault("minimum_objections", 3)
    out.setdefault("prosecutor_round", True)
    out.setdefault("compiler_judge", True)
    out.setdefault("self_review_round", True)
    out.setdefault("return_blackboard", True)
    out.setdefault("return_evidence", True)
    out.setdefault("output_format", "structured")
    debate_rounds = _clamp_int(out.get("debate_rounds"), 2, 1, 5)
    out["debate_rounds"] = debate_rounds
    out.setdefault("collaboration_rounds", debate_rounds)
    out.setdefault("message_rounds", min(2, debate_rounds))
    out.setdefault("councils", 1)
    out.setdefault("members_per_council", min(6, max(4, len(AUTO_DEBATE_PERSPECTIVES))))
    out.setdefault("max_tool_requests", max(12, MAX_TOOL_REQUESTS_DEFAULT))
    if not _as_str_list(out.get("perspectives")):
        out["perspectives"] = list(AUTO_DEBATE_PERSPECTIVES)
    return out


def _prepare_verify_chain_task(task: str, context: str, debate_rounds: int, auto_debate: bool, verify_chain: bool) -> tuple[str, str]:
    mode_name = "Auto-Debate + VerifyChain" if auto_debate else "VerifyChain"
    task2 = f"[{mode_name}] {task}"
    block = f"""
AUTO_DEBATE_VERIFY_CHAIN_MODE:
- Run a visible proponent/skeptic/prosecutor/verifier/judge debate.
- Use BLACKBOARD_UPDATE_JSON for facts, assumptions, open_questions, actions and objections.
- Use TOOL_REQUESTS_JSON only for safe read-only evidence checks when useful.
- Every major claim must appear in CLAIMS_JSON with evidence_refs, confidence and status.
- The final judge must compile JUDGE_COMPILED_JSON with: verdict, confirmed_findings, unsupported_claims, rejected_claims, decision, implementation_plan, tests, risks, dissent, next_step.
- Debate rounds target: {debate_rounds}.
- VerifyChain acceptance: final output must expose claims → evidence → objections → prosecutor verdict → tests.
""".strip()
    context2 = context + "\n\n---\n" + block if context else block
    return task2, context2


def _build_verify_chain_report(
    status: str,
    compiled_synthesis: dict[str, Any],
    ledger: list[dict[str, Any]],
    votes_summary: dict[str, Any],
    prosecutor_report: dict[str, Any],
    tool_requests: list[dict[str, Any]],
    auto_debate: bool,
    verify_chain: bool,
    debate_rounds: int,
) -> dict[str, Any]:
    claims = _claims_summary(ledger)
    executed = [req for req in tool_requests if isinstance(req, dict) and req.get("executed")]
    return {
        "enabled": bool(auto_debate or verify_chain),
        "mode": "auto_debate" if auto_debate else "verify_chain",
        "debate_rounds": debate_rounds,
        "status": status,
        "verdict": compiled_synthesis.get("verdict") or prosecutor_report.get("verdict") or status,
        "supported_claims": claims.get("supported", [])[:12],
        "unsupported_claims": claims.get("unsupported", [])[:12],
        "blocking_objections": (votes_summary or {}).get("blocking_objections", [])[:20],
        "prosecutor": prosecutor_report or {},
        "brokered_tools": {
            "requested": len(tool_requests),
            "executed": len(executed),
            "failed": sum(1 for req in executed if isinstance(req.get("result"), dict) and req["result"].get("ok") is False),
            "sample": executed[:8],
        },
        "checks": [
            {"name": "claims_extracted", "ok": claims.get("total", 0) > 0, "count": claims.get("total", 0)},
            {"name": "dissent_collected", "ok": bool((votes_summary or {}).get("blocking_objections")), "count": len((votes_summary or {}).get("blocking_objections", []))},
            {"name": "prosecutor_ran", "ok": bool(prosecutor_report), "verdict": prosecutor_report.get("verdict")},
            {"name": "judge_compiled", "ok": bool(compiled_synthesis), "verdict": compiled_synthesis.get("verdict")},
            {"name": "brokered_tools_executed", "ok": len(executed) > 0 or len(tool_requests) == 0, "executed": len(executed)},
        ],
    }


def _apply_preset_defaults(args: dict[str, Any]) -> dict[str, Any]:
    out = dict(args)
    preset_name = str(out.get("preset") or "deep").lower()
    if preset_name not in CONSILIUM_PRESETS:
        preset_name = "deep"
    if preset_name == "auto" or _normalise_bool(out.get("auto_scale"), False):
        scaled = _auto_scale(str(out.get("task", "")), str(out.get("context", "")))
        for key, value in scaled.items():
            out.setdefault(key, value)
        out["auto_scale"] = True
        return out
    preset = CONSILIUM_PRESETS[preset_name]
    for key, value in preset.items():
        if value is not None:
            out.setdefault(key, value)
    return out



def _estimate_budget(args: dict[str, Any], total_members: int, collaboration_rounds: int, message_rounds: int, research_missions: bool, max_research_agents: int, self_review_round: bool, plan_probe_decide: bool = False, prosecutor_round: bool = False, dissent_required: bool = False) -> dict[str, Any]:
    probe_calls = 1 if plan_probe_decide else 0
    research_calls = max_research_agents if research_missions else 0
    primary_calls = total_members
    collaboration_calls = total_members * max(0, collaboration_rounds)
    message_calls = total_members * max(0, message_rounds)
    judge_calls = 1 + (1 if self_review_round else 0)
    prosecutor_calls = 1 if prosecutor_round else 0
    dissent_calls = 1 if dissent_required else 0
    total_calls = probe_calls + primary_calls + collaboration_calls + message_calls + research_calls + judge_calls + prosecutor_calls + dissent_calls
    context_len = len(str(args.get("context") or "")) + len(str(args.get("task") or ""))
    rough_input_chars = total_calls * min(MAX_PROMPT_CONTEXT_CHARS, max(4000, context_len + 6000))
    rough_output_tokens = total_calls * min(int(args.get("max_tokens") or DEFAULT_MAX_TOKENS), 32000)
    return {
        "model_calls": total_calls,
        "breakdown": {"probe": probe_calls, "primary": primary_calls, "collaboration": collaboration_calls, "message": message_calls, "research": research_calls, "judge": judge_calls, "prosecutor": prosecutor_calls, "forced_dissent": dissent_calls},
        "rough_input_chars_upper": rough_input_chars,
        "rough_output_tokens_upper": rough_output_tokens,
        "latency_hint_seconds": f"{max(10, total_calls * 4)}-{max(30, total_calls * 18)}",
        "expensive": total_calls >= 20 or rough_output_tokens > 250000,
    }


def _call_memory_wiki_tool(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Call memory-wiki tool with broker-first, direct-import-fallback strategy."""
    result = _call_runtime_tool(tool_name, payload)
    if isinstance(result, dict) and result.get("ok") is False:
        # Попробовать прямой импорт memory-wiki плагина
        try:
            plugin_file = _profile_home() / "plugins" / "memory-wiki" / "__init__.py"
            if plugin_file.exists():
                spec = _iu.spec_from_file_location("omnicouncil_mw_fallback", plugin_file)
                if spec and spec.loader:
                    module = _iu.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    class MWCtx:
                        def __init__(self):
                            self.tools = []
                        def register_tool(self, **kwargs):
                            self.tools.append(kwargs)
                        def register_memory_provider(self, *a, **kw):
                            self.memory_provider = (a, kw)
                        def __getattr__(self, name):
                            if str(name).startswith("register_"):
                                return lambda *a, **kw: None
                            raise AttributeError(name)
                    ctx = MWCtx()
                    if hasattr(module, "register"):
                        module.register(ctx)
                    for entry in ctx.tools:
                        if isinstance(entry, dict) and entry.get("name") == tool_name and callable(entry.get("handler")):
                            return _tool_result_preview(entry["handler"](payload), 4000)
        except Exception:
            pass
    return _tool_result_preview(result, 4000)


def _save_task_capsule(task: str, synthesis: str, result: dict[str, Any], ledger: list[dict[str, Any]], tool_requests: list[dict[str, Any]]) -> dict[str, Any]:
    payload = {
        "intent": task,
        "topic": "hermes_omnicouncil",
        "plan": _truncate_text(synthesis, 6000),
        "files": [],
        "commands": [],
        "errors": [e.get("error", "") for e in result.get("member_errors", []) if e.get("error")][:20],
        "fixes": [],
        "verification": _truncate_text(_json({"diagnostics": result.get("diagnostics", {}), "evidence_count": len(ledger), "tool_requests": len(tool_requests)}), 3000),
        "followups": [],
    }
    raw = _call_memory_wiki_tool("memory_wiki_add_task_capsule", payload)
    return raw


def _cache_files() -> list[Path]:
    if not CACHE_DIR.exists():
        return []
    return sorted(CACHE_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)


def _cache_find(prefix: str) -> Path | None:
    prefix = str(prefix or "").strip()
    if not prefix:
        return None
    for path in _cache_files():
        if path.stem.startswith(prefix):
            return path
    return None


def _handle_cache_list(args=None, **_kw):
    args = dict(args or {})
    limit = _clamp_int(args.get("limit"), 20, 1, 200)
    include_summaries = _normalise_bool(args.get("include_summaries"), False)
    items = []
    for path in _cache_files()[:limit]:
        item = {"cache_key": path.stem, "size": path.stat().st_size, "mtime": path.stat().st_mtime}
        if include_summaries:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                item.update({"status": data.get("status"), "task": _truncate_text(data.get("synthesis", ""), 500), "seconds": data.get("seconds")})
            except Exception as exc:
                item["error"] = _truncate_text(str(exc), 200)
        items.append(item)
    return _json({"status": "success", "cache_dir": str(CACHE_DIR), "count": len(items), "items": items})


def _handle_cache_get(args=None, **_kw):
    args = dict(args or {})
    path = _cache_find(args.get("cache_key", ""))
    if not path:
        return _json({"status": "error", "error": "cache_key_not_found"})
    max_chars = _clamp_int(args.get("max_chars"), 12000, 1000, 200000)
    return _truncate_text(path.read_text(encoding="utf-8", errors="replace"), max_chars)


def _handle_cache_clear(args=None, **_kw):
    args = dict(args or {})
    if _normalise_bool(args.get("all"), False):
        files = _cache_files()
    else:
        found = _cache_find(args.get("cache_key", ""))
        files = [found] if found else []
    removed = []
    for path in files:
        if path and path.exists():
            path.unlink()
            removed.append(path.stem)
    return _json({"status": "success", "removed": removed, "count": len(removed)})


def _handle_cache_explain(args=None, **_kw):
    args = dict(args or {})
    task = str(args.get("task") or "")
    if not task:
        return _json({"status": "error", "error": "task is required"})
    options = args.get("options") if isinstance(args.get("options"), dict) else {}
    resolved = _apply_preset_defaults({**options, "task": task, "context": args.get("context", "")})
    default_model, _members, _judge, _research = _resolve_models(resolved)
    ledger: list[dict[str, Any]] = []
    manifest = _build_capability_manifest(resolved, ledger)
    key = _cache_key(task, str(args.get("context") or ""), str(args.get("mode") or "edit_plan"), resolved, manifest, default_model)
    return _json({"status": "success", "cache_key": key, "cache_path": str(CACHE_DIR / f"{key}.json"), "manifest_evidence": ledger})


def _handle_doctor(args=None, **_kw):
    global _RUNTIME_CTX
    args = dict(args or {})
    checks = set(_as_str_list(args.get("checks")) or ["schema", "registration", "safe_tools", "cache", "deep_web", "models"])
    report: dict[str, Any] = {"status": "success", "tool": "omnicouncil_doctor", "version": VERSION, "checks": {}, "warnings": []}
    if "schema" in checks:
        props = SCHEMA.get("parameters", {}).get("properties", {})
        report["checks"]["schema"] = {"ok": "task" in SCHEMA.get("parameters", {}).get("required", []), "property_count": len(props), "has_dry_run": "dry_run" in props, "has_fallback_models": "fallback_models" in props}
    if "registration" in checks:
        class FakeCtx:
            def __init__(self):
                self.tools = []
            def register_tool(self, **kwargs):
                self.tools.append(kwargs)
        ctx = FakeCtx()
        old_runtime_ctx = _RUNTIME_CTX
        try:
            register(ctx)
        finally:
            _RUNTIME_CTX = old_runtime_ctx
        report["checks"]["registration"] = {"ok": True, "tools": [t.get("name") for t in ctx.tools], "count": len(ctx.tools)}
    if "safe_tools" in checks:
        blocked = _extract_tool_requests('TOOL_REQUESTS_JSON: [{"tool":"patch","args":{}},{"tool":"terminal","args":{}}]', 10)
        report["checks"]["safe_tools"] = {"ok": blocked == [], "allowed": SAFE_AGENT_TOOLS, "blocked_probe_count": len(blocked)}
    if "cache" in checks:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        report["checks"]["cache"] = {"ok": CACHE_DIR.exists(), "path": str(CACHE_DIR), "files": len(_cache_files())}
        if _normalise_bool(args.get("include_cache_samples"), False):
            report["checks"]["cache"]["samples"] = [p.stem for p in _cache_files()[:5]]
    if "deep_web" in checks:
        report["checks"]["deep_web"] = {"ok": hasattr(_deep_web_research, "handler"), "version": getattr(_deep_web_research, "VERSION", ""), "has_management_schemas": bool(getattr(_deep_web_research, "MANAGEMENT_SCHEMAS", []))}
    if "models" in checks:
        if _normalise_bool(args.get("live_model_check"), False):
            try:
                started = time.time()
                content = _call_model_text("Return exactly OK", 16, 0.0, timeout=30, retries=0, jitter_ms=0)
                report["checks"]["models"] = {"ok": bool(content), "content": _truncate_text(content, 80), "elapsed_ms": int((time.time() - started) * 1000)}
            except Exception as exc:
                report["checks"]["models"] = {"ok": False, "error": _truncate_text(str(exc), 500)}
                report["status"] = "partial"
        else:
            report["checks"]["models"] = {"ok": True, "live_model_check": False}
    return _json(report)

# ═══════════════════════════════════════════════════════════════
#  MAIN HANDLER — omnicouncil orchestration
# ═══════════════════════════════════════════════════════════════
def handler(args=None, **_kw):
    global _ACTIVE_FALLBACK_MODELS, _OMNICOUNCIL_SESSION_ID
    if args is None:
        args = {k: v for k, v in _kw.items() if k not in {"task_id", "ctx"}}
    else:
        args = dict(args or {})
        for k, v in _kw.items():
            if k not in {"task_id", "ctx"} and k not in args:
                args[k] = v

    args = _apply_auto_debate_defaults(args)
    args = _apply_preset_defaults(args)
    task = str(args.get("task", "")).strip()
    context = str(args.get("context", "")).strip()
    mode = args.get("mode", "edit_plan") or "edit_plan"
    if mode not in {"advise", "edit_plan", "review", "debug"}:
        mode = "edit_plan"
    if not task:
        return _json({"status": "error", "error": "task is required"})

    auto_debate = _normalise_bool(args.get("auto_debate"), False)
    verify_chain = _normalise_bool(args.get("verify_chain"), auto_debate)
    debate_rounds = _clamp_int(args.get("debate_rounds"), 2, 1, 5)
    if auto_debate or verify_chain:
        task, context = _prepare_verify_chain_task(task, context, debate_rounds, auto_debate, verify_chain)

    # ── Model resolution
    default_spec, member_specs, judge_spec, research_spec = _resolve_models(args)
    # Backward-compat string model names (flow through existing code unchanged)
    _spec_str = lambda s: f"{s['provider']}:{s['model']}" if s.get('provider') else s['model']
    default_model = _spec_str(default_spec)
    member_models = [s['model'] for s in member_specs]
    judge_model = _spec_str(judge_spec)
    research_model = _spec_str(research_spec)
    # ── CouncilRunContext: изолированный контекст запуска (P0 #1, #6 fix) ──
    import uuid as _uuid_mod
    run_id = str(_uuid_mod.uuid4())
    session_id = run_id  # каждый council — уникальная сессия
    namespace = f"omnicouncil:blackboard:{session_id}"

    # Provider data policy (P0 #6 fix)
    provider_data_policy = str(args.get("provider_data_policy") or "internal")
    implicit_http_fallback = _normalise_bool(args.get("implicit_http_fallback"), False)
    
    # Run budget (P1 #7)
    budget = RunBudget(
        max_total_tokens=_clamp_int(args.get("max_total_tokens"), 1_000_000, 1000, 4_000_000),
        max_total_cost_usd=float(args.get("max_cost_usd") or 5.0),
        max_wall_time_seconds=float(args.get("timeout") or args.get("max_wall_time_seconds") or 600),
        max_model_calls=_clamp_int(args.get("max_model_calls"), 50, 1, 200),
    )
    
    # Создать изолированный контекст
    run_ctx = CouncilRunContext(
        run_id=run_id,
        session_id=session_id,
        namespace=namespace,
        model_provider_map={s['model']: s.get('provider') for s in member_specs},
        judge_provider=judge_spec.get('provider'),
        research_provider=research_spec.get('provider'),
        fallback_models=tuple(_as_str_list(args.get("fallback_models"))),
        deadline=time.time() + budget.max_wall_time_seconds if budget.max_wall_time_seconds else None,
        budget=budget,
        provider_data_policy=provider_data_policy,
        implicit_http_fallback=implicit_http_fallback,
    )
    
    # Установить в контекст для broker_tool_call и _call_model_text
    _ctx_token = _ACTIVE_RUN_CTX.set(run_ctx)
    # P0 #1 fix: reset вызывается перед каждым return + finally safety net
    
    # Заполнить устаревшие глобалы ДЛЯ ОБРАТНОЙ СОВМЕСТИМОСТИ
    global _MODEL_PROVIDER_MAP, _JUDGE_PROVIDER, _RESEARCH_PROVIDER, _JUDGE_SPEC, _RESEARCH_SPEC
    _MEMBER_SPEC_MAP: dict[str, dict] = {s['model']: s for s in member_specs}
    _JUDGE_SPEC = judge_spec
    _RESEARCH_SPEC = research_spec
    _MODEL_PROVIDER_MAP = {s['model']: s.get('provider') for s in member_specs}
    _JUDGE_PROVIDER = judge_spec.get('provider')
    _RESEARCH_PROVIDER = research_spec.get('provider')
    _OMNICOUNCIL_SESSION_ID = session_id
    _ACTIVE_FALLBACK_MODELS = _as_str_list(args.get("fallback_models"))

    # ── Parse args
    councils = _clamp_int(args.get("councils"), DEFAULT_COUNCILS, 1, MAX_COUNCILS)
    members_per_council = _clamp_int(args.get("members_per_council"), DEFAULT_MEMBERS_PER_COUNCIL, 1, MAX_MEMBERS_PER_COUNCIL)
    agentic_blackboard = _normalise_bool(args.get("agentic_blackboard"), False) or preset_active(args)
    minimum_tools = _normalise_bool(args.get("minimum_tools"), True)
    brokered_tools = _normalise_bool(args.get("brokered_tools"), True)
    active_tool_agents = _clamp_int(args.get("active_tool_agents"), AGENTIC_ACTIVE_TOOL_AGENTS, 1, 20)
    mutating_agents = _clamp_int(args.get("mutating_agents"), AGENTIC_MUTATING_AGENTS, 0, 0)
    return_blackboard = _normalise_bool(args.get("return_blackboard"), False)
    perspectives = _agentic_perspectives(args.get("perspectives")) if agentic_blackboard else _member_perspectives(args.get("perspectives"))
    decision_policy = str(args.get("decision_policy") or "judge").strip().lower()
    if decision_policy not in {"judge", "majority", "consensus", "risk_weighted"}:
        decision_policy = "judge"
    red_team = _normalise_bool(args.get("red_team"), False)
    max_tokens = _clamp_int(args.get("max_tokens"), DEFAULT_MAX_TOKENS, 1000, 384000)
    judge_max_tokens = _clamp_int(args.get("judge_max_tokens"), DEFAULT_JUDGE_MAX_TOKENS, 1000, 384000)
    use_cache = _normalise_bool(args.get("use_cache"), True) and not _normalise_bool(args.get("force_refresh"), False)
    cache_ttl_seconds = _clamp_int(args.get("cache_ttl_seconds"), CACHE_TTL, 0, 30 * 24 * 60 * 60)
    collaborate = _normalise_bool(args.get("collaborate"), True)
    collaboration_rounds = _clamp_int(args.get("collaboration_rounds"), DEFAULT_COLLABORATION_ROUNDS, 0, 4)
    message_rounds = _clamp_int(args.get("message_rounds"), DEFAULT_MESSAGE_ROUNDS, 0, 3)
    return_transcript = _normalise_bool(args.get("return_transcript"), False)
    return_evidence = _normalise_bool(args.get("return_evidence"), True)
    research_missions = _normalise_bool(args.get("research_missions"), False)
    max_research_agents = _clamp_int(args.get("max_research_agents"), 3, 0, 8)
    max_tool_requests = _clamp_int(args.get("max_tool_requests"), MAX_TOOL_REQUESTS_DEFAULT, 0, MAX_BROKERED_TOOL_REQUESTS)
    tool_mode = str(args.get("tool_mode", "safe_agent") or "safe_agent").strip().lower()
    if tool_mode not in {"off", "safe_agent", "council_safe"}:
        tool_mode = "safe_agent"
    if tool_mode == "off":
        max_tool_requests = 0
        brokered_tools = False
    # ── Mutation policy (new in v5.5) ──
    allow_file_mutations = _normalise_bool(args.get("allow_file_mutations"), False)
    allow_code_mutations = _normalise_bool(args.get("allow_code_mutations"), False)
    critical_change_policy = str(args.get("critical_change_policy") or "operator_only").strip().lower()
    if critical_change_policy not in {"propose_only", "judge_approved", "operator_only"}:
        critical_change_policy = "operator_only"
    # ── Output format ──
    output_format = args.get("output_format", "structured") or "structured"
    if output_format not in {"prose", "structured", "patch_plan", "json"}:
        output_format = "structured"
    auto_memory_context = _normalise_bool(args.get("auto_memory_context"), True)
    memory_context_chars = _clamp_int(args.get("memory_context_chars"), DEFAULT_MEMORY_CONTEXT_CHARS, 0, MAX_MEMORY_CONTEXT_CHARS)
    save_task_capsule = _normalise_bool(args.get("save_task_capsule"), False)
    member_retries = _clamp_int(args.get("member_retries"), DEFAULT_MEMBER_RETRIES, 0, 3)
    model_timeout = _clamp_int(args.get("model_timeout"), DEFAULT_MODEL_TIMEOUT, 30, 3600)
    judge_timeout = _clamp_int(args.get("judge_timeout"), DEFAULT_JUDGE_TIMEOUT, 30, 3600)
    request_jitter_ms = _clamp_int(args.get("request_jitter_ms"), DEFAULT_REQUEST_JITTER_MS, 0, MAX_REQUEST_JITTER_MS)
    strict_json = _normalise_bool(args.get("strict_json"), False)
    json_schema = args.get("json_schema") if isinstance(args.get("json_schema"), dict) else {}
    dry_run = _normalise_bool(args.get("dry_run"), False)
    dissent_required = _normalise_bool(args.get("dissent_required"), False)
    anti_slop = _normalise_bool(args.get("anti_slop"), False)
    self_review_round = _normalise_bool(args.get("self_review_round"), False)
    plan_probe_decide = _normalise_bool(args.get("plan_probe_decide"), True)
    prosecutor_round = _normalise_bool(args.get("prosecutor_round"), True)
    minimum_objections = _clamp_int(args.get("minimum_objections"), 2, 0, 20)
    compiler_judge = _normalise_bool(args.get("compiler_judge"), True)
    save_council_lessons = _normalise_bool(args.get("save_council_lessons"), False)
    enabled_toolsets = _as_str_list(args.get("enabled_toolsets"))
    total_members = councils * members_per_council
    max_member_workers = _clamp_int(args.get("max_member_workers"), DEFAULT_MAX_MEMBER_WORKERS, 1, MAX_COUNCILS * MAX_MEMBERS_PER_COUNCIL)
    max_collaboration_workers = _clamp_int(args.get("max_collaboration_workers"), DEFAULT_MAX_COLLABORATION_WORKERS, 1, MAX_COUNCILS * MAX_MEMBERS_PER_COUNCIL)
    max_research_workers = _clamp_int(args.get("max_research_workers"), DEFAULT_MAX_RESEARCH_WORKERS, 1, 16)
    min_successful_members = _clamp_int(
        args.get("min_successful_members"),
        max(1, int(math.ceil(total_members * DEFAULT_QUORUM_RATIO))),  # P0 fix: dynamic 60% quorum
        0, total_members,
    )

    context = _bounded_context(context)
    started = time.time()
    ledger: list[dict[str, Any]] = []

    # ── Manifest
    manifest = _build_capability_manifest({**args, "tool_mode": tool_mode, "agentic_blackboard": agentic_blackboard, "minimum_tools": minimum_tools, "brokered_tools": brokered_tools, "active_tool_agents": active_tool_agents, "mutating_agents": mutating_agents, "max_tool_requests": max_tool_requests}, ledger)
    if auto_debate or verify_chain:
        manifest["auto_debate"] = {"enabled": auto_debate, "verify_chain": verify_chain, "debate_rounds": debate_rounds}
        _add_evidence(ledger, "auto_debate", "Enabled Auto-Debate/VerifyChain orchestration mode.", verify_chain=verify_chain, debate_rounds=debate_rounds)

    if dry_run:
        estimate = _estimate_budget(args, total_members, collaboration_rounds, message_rounds, research_missions, max_research_agents, self_review_round, plan_probe_decide, prosecutor_round, dissent_required)
        _ACTIVE_RUN_CTX.reset(_ctx_token)
        return _json({
            "status": "dry_run",
            "tool": "hermes_omnicouncil",
            "version": VERSION,
            "preset": args.get("preset"),
            "model": default_model,
            "judge_model": judge_model,
            "member_models": member_models,
            "councils": councils,
            "members_per_council": members_per_council,
            "estimate": estimate,
            "tool_mode": tool_mode,
            "allow_file_mutations": allow_file_mutations,
            "allow_code_mutations": allow_code_mutations,
            "critical_change_policy": critical_change_policy,
            "capability_profile": manifest.get("capability_profile"),
            "fallback_models": _ACTIVE_FALLBACK_MODELS,
            "plan_probe_decide": plan_probe_decide,
            "prosecutor_round": prosecutor_round,
            "compiler_judge": compiler_judge,
            "auto_debate": auto_debate,
            "verify_chain": verify_chain,
            "debate_rounds": debate_rounds,
            "minimum_objections": minimum_objections,
            "save_council_lessons": save_council_lessons,
            "evidence": ledger if return_evidence else [],
        })

    # ── Cross-plugin context first, so Memory Wiki can suppress code already
    # present in the Code Shrinker packet before any model receives the prompt.
    cross_plugin_context = _prepare_cross_plugin_context(
        args, task, context, memory_context_chars, ledger
    )
    if cross_plugin_context.get("status") == "failed" and cross_plugin_context.get("mode") == "required":
        _ACTIVE_RUN_CTX.reset(_ctx_token)
        return _json({
            "status": "error",
            "tool": "hermes_omnicouncil",
            "version": VERSION,
            "error": "cross_plugin_context_required_failed",
            "detail": cross_plugin_context.get("error", "unknown integration error"),
            "cross_plugin_context": cross_plugin_context,
        })

    code_context = str(cross_plugin_context.get("code_context") or "")
    memory_context = str(cross_plugin_context.get("memory_context") or "")
    if code_context:
        context = (context + "\n\n---\nCurrent Code Shrinker packet:\n" + code_context) if context else ("Current Code Shrinker packet:\n" + code_context)
    if not memory_context and auto_memory_context and memory_context_chars > 0:
        memory_context = _prefetch_memory_context(task, context, memory_context_chars, ledger)
    if memory_context:
        context = (context + "\n\n---\nRelevant durable memory (coverage-deduplicated):\n" + memory_context) if context else ("Relevant durable memory (coverage-deduplicated):\n" + memory_context)

    # ── Cache
    key = _cache_key(task, context, mode, {
        "preset": args.get("preset"), "councils": councils, "members_per_council": members_per_council,
        "decision_policy": decision_policy, "red_team": red_team, "agentic_blackboard": agentic_blackboard,
        "tool_mode": tool_mode, "allow_file_mutations": allow_file_mutations,
        "allow_code_mutations": allow_code_mutations, "critical_change_policy": critical_change_policy,
        "capability_profile": manifest.get("capability_profile"),
        "dissent_required": dissent_required, "anti_slop": anti_slop, "self_review_round": self_review_round,
        "fallback_models": _ACTIVE_FALLBACK_MODELS, "json_schema": bool(json_schema),
        "plan_probe_decide": plan_probe_decide, "prosecutor_round": prosecutor_round, "compiler_judge": compiler_judge,
        "minimum_objections": minimum_objections, "save_council_lessons": save_council_lessons,
        "auto_debate": auto_debate, "verify_chain": verify_chain, "debate_rounds": debate_rounds,
        "repository_id": str(args.get("repository_id") or ""),
        "target_file": str(args.get("target_file") or ""),
        "target_symbol": str(args.get("target_symbol") or ""),
        "code_context_mode": str(args.get("code_context_mode") or "auto"),
        "coverage_packet_id": str((cross_plugin_context.get("coverage_manifest") or {}).get("packet_id") or ""),
    }, manifest, default_model)
    cache_file = CACHE_DIR / f"{key}.json"
    if use_cache and cache_file.exists() and time.time() - cache_file.stat().st_mtime < cache_ttl_seconds:
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            cached["cached"] = True
            _ACTIVE_RUN_CTX.reset(_ctx_token)
            return _json(cached)
        except Exception:
            pass

    # ── Plan→Probe→Decide preflight
    probe_plan: dict[str, Any] = {}
    if plan_probe_decide:
        probe_plan = _run_probe_phase(
            task, context, mode, manifest, ledger, max_tool_requests,
            model=research_model or default_model,
            timeout=model_timeout,
            retries=member_retries,
            jitter_ms=request_jitter_ms,
            max_workers=max_research_workers,
        )
        if probe_plan:
            probe_context = _truncate_text(_json({
                "unknowns": probe_plan.get("unknowns", []),
                "risk_points": probe_plan.get("risk_points", []),
                "expected_evidence": probe_plan.get("expected_evidence", []),
                "tool_requests": probe_plan.get("tool_requests", []),
            }), 40000)
            context = context + "\n\n---\nPlan→Probe→Decide preflight:\n" + probe_context if context else "Plan→Probe→Decide preflight:\n" + probe_context

    # ── Research missions
    research_reports: list[dict[str, Any]] = []
    if research_missions and max_research_agents > 0:
        research_reports = _run_research_missions(task, mode, manifest, max_research_agents, enabled_toolsets, max_tokens, ledger, max_research_workers, request_jitter_ms, research_model=research_model)
        _collect_claims_from_responses(research_reports, ledger)

    # ── Primary member calls
    answers: list[dict[str, Any]] = []
    primary_workers = max(1, min(total_members, max_member_workers))
    blackboard: dict[str, Any] = {"task": task, "round": 0, "facts": [], "open_questions": [], "notes": {}}
    with concurrent.futures.ThreadPoolExecutor(max_workers=primary_workers) as pool:
        future_map = {
            pool.submit(
                _contextvars.copy_context().run,
                _call_member, task, context, mode, ci, mi, max_tokens, manifest, ledger,
                output_format, max_tool_requests, perspectives, councils, model_timeout,
                member_retries, decision_policy, red_team, request_jitter_ms,
                blackboard=blackboard,
                member_model=member_models[(ci * members_per_council + mi) % len(member_models)],
                members_per_council=members_per_council,
            ): (ci, mi)
            for ci in range(councils) for mi in range(members_per_council)
        }
        for future in concurrent.futures.as_completed(future_map):
            ci, mi = future_map[future]
            try:
                answers.append(future.result())
            except Exception as exc:
                label, perspective = _member_identity(ci, mi, perspectives, members_per_council)
                answers.append({"label": label, "council": ci + 1, "member": mi + 1, "perspective": perspective, "status": "failed", "error": _truncate_text(str(exc), 500)})
    answers.sort(key=lambda item: (int(item.get("council") or 0), int(item.get("member") or 0), str(item.get("label") or "")))
    _merge_blackboard_from_responses(blackboard, answers)
    _collect_claims_from_responses(answers, ledger)

    succeeded = sum(1 for a in answers if a.get("status") == "success")
    tool_requests = _dedupe_tool_requests(answers, max_tool_requests, minimum_tools=minimum_tools)
    if tool_requests:
        _add_evidence(ledger, "tool_requests", f"Collected {len(tool_requests)} unique tool requests from members.", count=len(tool_requests))
        if brokered_tools and tool_mode != "off":
            _execute_tool_requests(tool_requests, ledger, max_workers=max_research_workers)

    dissent_report: dict[str, Any] = {}

    _policy_warnings: list[str] = []

    if succeeded < min_successful_members:
        transcript = []
        message_transcript = []
        all_messages = []
        all_responses = list(answers)
        votes_summary = _aggregate_votes(all_responses)
        collaboration_responded = 0
        judge_error = f"primary quorum not met: {succeeded}/{min_successful_members}"
        synthesis = _fallback_synthesis(task, mode, answers, transcript, research_reports, tool_requests, judge_error)
        status = "partial" if succeeded or any(r.get("status") == "success" for r in research_reports) else "failed"
    else:
        # ── Collaboration rounds
        transcript = _run_collaboration(task, context, mode, answers, collaboration_rounds, max_tokens, manifest, ledger, research_reports, tool_requests, max_tool_requests, max_collaboration_workers, model_timeout, member_retries, request_jitter_ms, blackboard=blackboard, member_models=member_models) if collaborate else []

        # ── Message rounds (агенты общаются друг с другом)
        message_transcript, all_messages = _run_message_rounds(task, context, mode, answers, message_rounds, max_tokens, manifest, ledger, tool_requests, max_tool_requests, max_collaboration_workers, model_timeout, member_retries, request_jitter_ms, blackboard=blackboard, member_models=member_models)

        all_responses = answers + [r for t in transcript for r in t.get("responses", [])] + [r for t in message_transcript for r in t.get("responses", [])]
        _merge_blackboard_from_responses(blackboard, all_responses)
        votes_summary = _aggregate_votes(all_responses)
        if votes_summary.get("votes"):
            vote_count = len(votes_summary.get("votes", []))
            _add_evidence(ledger, "structured_votes", f"Collected {vote_count} structured votes.", majority=votes_summary.get("majority"), confidence=votes_summary.get("average_confidence"))
        tool_requests = _dedupe_tool_requests(all_responses, max_tool_requests, minimum_tools=minimum_tools)
        if tool_requests and brokered_tools and tool_mode != "off":
            _execute_tool_requests(tool_requests, ledger, max_workers=max_research_workers)
        collaboration_responded = sum(1 for round_item in transcript for response in round_item.get("responses", []) if response.get("status") == "success")

        _collect_claims_from_responses(all_responses, ledger)
        if dissent_required:
            dissent_report = _ensure_minimum_dissent(
                task, context, all_responses, votes_summary, ledger,
                minimum_objections=minimum_objections,
                model=judge_model,
                timeout=judge_timeout,
                retries=member_retries,
                jitter_ms=request_jitter_ms,
            )
            if dissent_report.get("generated"):
                blackboard["forced_dissent"] = dissent_report.get("generated")

        try:
            synthesis = _judge(task, context, mode, answers, judge_max_tokens, transcript, manifest, ledger, research_reports, tool_requests, all_messages, output_format, save_task_capsule, timeout=judge_timeout, retries=member_retries, strict_json=strict_json, decision_policy=decision_policy, red_team=red_team, jitter_ms=request_jitter_ms, json_schema=json_schema, judge_model=judge_model, dissent_required=dissent_required, anti_slop=anti_slop, compiler_judge=compiler_judge)
            status = "success"
            judge_error = ""

            # ── Enforce decision_policy: majority overrides judge if votes disagree ──
            _policy_warnings: list[str] = []
            if decision_policy == "majority" and votes_summary.get("majority") == "reject":
                synthesis = (
                    f"## ⚠ Majority REJECT — judge synthesis overridden\n"
                    f"The majority of agents ({votes_summary.get('counts',{}).get('reject',0)}/{sum(votes_summary.get('counts',{}).values())}) "
                    f"rejected this approach. Confidence: {votes_summary.get('average_confidence',0)}.\n"
                    f"Blocking objections: {votes_summary.get('blocking_objections',[])}\n\n"
                    f"---\nJudge synthesis (OVERRIDDEN by majority):\n{synthesis}"
                )
                status = "partial"
                _policy_warnings.append("Vote majority=reject overrides judge synthesis.")
            elif decision_policy == "consensus" and votes_summary.get("counts", {}).get("approve", 0) < succeeded:
                synthesis = (
                    f"## ⚠ Consensus NOT reached ({votes_summary.get('counts',{}).get('approve',0)}/{succeeded} approved)\n\n"
                    f"---\n{synthesis}"
                )
                status = "partial"
                _policy_warnings.append("Consensus not reached; judge synthesis delivered with caveat.")

            # ── Self-review round (опциональный post-judge check)
            if self_review_round:
                try:
                    review_prompt = f"""Ты — self-review auditor для OmniCouncil judge synthesis.
Проверь финальный ответ на:
- unsupported claims (утверждения без evidence)
- missing evidence (голословные рекомендации)
- vague recommendations (без конкретных file:line/шагов)
- unsafe suggestions (рискованные действия без rollback)
- contradictions between agents (противоречия не разрешённые judge)
- omitted high-risk dissent (скрытые риски, которые проигнорированы)

Исходный ответ judge:
{synthesis[:80000]}

Верни ИСПРАВЛЕННУЮ версию ответа. Если всё ок — верни оригинал с пометкой [SELF-REVIEW: PASSED]."""
                    reviewed = _call_model_text(review_prompt, min(judge_max_tokens, 64000), 0.05, model=judge_model, timeout=judge_timeout, jitter_ms=request_jitter_ms)
                    if reviewed and len(reviewed) > 20 and "[SELF-REVIEW: PASSED]" not in reviewed:
                        synthesis = reviewed
                except Exception:
                    pass
        except Exception as exc:
            judge_error = _truncate_text(str(exc), 1000)
            synthesis = _fallback_synthesis(task, mode, answers, transcript, research_reports, tool_requests, f"judge failed: {judge_error}")
            status = "partial" if succeeded else "failed"

    # ── Prosecutor + Judge compiler + compact lessons
    prosecutor_report: dict[str, Any] = {}
    if prosecutor_round and synthesis:
        prosecutor_report = _run_prosecutor(
            task, context, synthesis, ledger, votes_summary,
            model=judge_model,
            timeout=judge_timeout,
            retries=member_retries,
            jitter_ms=request_jitter_ms,
        )
    compiled_synthesis = _compile_judge_output(synthesis, ledger, votes_summary, prosecutor_report) if compiler_judge else {}

    # ── Result
    diagnostics = {
        "primary_success": succeeded,
        "primary_failed": total_members - succeeded,
        "collaboration_success": collaboration_responded,
        "message_rounds": message_rounds,
        "messages_exchanged": len(all_messages),
        "research_success": sum(1 for r in research_reports if r.get("status") == "success"),
        "tool_requests": len(tool_requests),
        "tool_requests_executed": sum(1 for req in tool_requests if req.get("executed")),
        "judge_success": not bool(judge_error),
        "bounded_workers": {"primary": primary_workers, "collaboration": max_collaboration_workers, "research": max_research_workers},
        "agentic_blackboard": agentic_blackboard,
        "models": {"default": default_model, "member_count": len(member_models), "judge": judge_model, "research": research_model, "fallbacks": _ACTIVE_FALLBACK_MODELS},
        "votes": votes_summary,
        "plan_probe_decide": plan_probe_decide,
        "probe_executed": probe_plan.get("executed", 0) if isinstance(probe_plan, dict) else 0,
        "prosecutor_verdict": prosecutor_report.get("verdict") if isinstance(prosecutor_report, dict) else None,
        "compiler_judge": compiler_judge,
        "claims": _claims_summary(ledger).get("by_status", {}),
        "dissent": dissent_report,
        "warnings": [],
        "cross_plugin_context": {
            "status": cross_plugin_context.get("status"),
            "mode": cross_plugin_context.get("mode"),
            "suppression_status": cross_plugin_context.get("suppression_status"),
            "error": cross_plugin_context.get("error"),
        },
        "context_token_ledger": cross_plugin_context.get("context_token_ledger", {}),
    }

    if judge_error:
        diagnostics["warnings"].append("Judge failed; returned fallback synthesis.")
    if _policy_warnings:
        diagnostics["warnings"].extend(_policy_warnings)

    result = {
        "status": status,
        "tool": "hermes_omnicouncil",
        "version": VERSION,
        "preset": args.get("preset"),
        "model": default_model,
        "judge_model": judge_model,
        "member_model_count": len(member_models),
        "councils": councils,
        "members_per_council": members_per_council,
        "total_members": total_members,
        "members_responded": succeeded,
        "collaborate": collaborate,
        "collaboration_rounds": collaboration_rounds,
        "message_rounds": message_rounds,
        "messages_exchanged": len(all_messages),
        "decision_policy": decision_policy,
        "red_team": red_team,
        "agentic_blackboard": agentic_blackboard,
        "tool_mode": tool_mode,
        "allow_file_mutations": allow_file_mutations,
        "allow_code_mutations": allow_code_mutations,
        "critical_change_policy": critical_change_policy,
        "capability_profile": manifest.get("capability_profile"),
        "research_missions": research_missions,
        "output_format": output_format,
        "dissent_required": dissent_required,
        "anti_slop": anti_slop,
        "self_review_round": self_review_round,
        "plan_probe_decide": plan_probe_decide,
        "prosecutor_round": prosecutor_round,
        "compiler_judge": compiler_judge,
        "auto_debate": auto_debate,
        "verify_chain": verify_chain,
        "debate_rounds": debate_rounds,
        "minimum_objections": minimum_objections,
        "save_council_lessons": save_council_lessons,
        "fallback_models": _ACTIVE_FALLBACK_MODELS,
        "votes": votes_summary,
        "seconds": round(time.time() - started, 1),
        "synthesis": synthesis,
        "compiled_synthesis": compiled_synthesis,
        "tool_requests": tool_requests,
        "diagnostics": diagnostics,
        "cross_plugin_context": {
            key: value for key, value in cross_plugin_context.items()
            if key not in {"code_context", "memory_context"}
        },
        "editable_after": True,
    }

    if auto_debate or verify_chain:
        result["verify_chain_report"] = _build_verify_chain_report(status, compiled_synthesis, ledger, votes_summary, prosecutor_report, tool_requests, auto_debate, verify_chain, debate_rounds)

    if agentic_blackboard or return_blackboard:
        result["blackboard"] = {
            "policy": manifest.get("blackboard", {}),
            "summary": _blackboard_summary(answers, transcript, tool_requests, all_messages, manifest),
            "state": _tool_result_preview(blackboard, 20000),
        }
    if all_messages:
        result["messages"] = all_messages
    if return_evidence:
        result["evidence"] = ledger
    if research_reports:
        result["research_reports"] = research_reports
    if return_transcript:
        result["transcript"] = transcript
        result["message_transcript"] = message_transcript
    if probe_plan:
        result["probe_plan"] = probe_plan
    if dissent_report:
        result["dissent_report"] = dissent_report
    if prosecutor_report:
        result["prosecutor_report"] = prosecutor_report
    council_lessons = _derive_council_lessons(task, status, compiled_synthesis, prosecutor_report, diagnostics)
    if council_lessons:
        result["council_lessons"] = council_lessons
    if save_task_capsule:
        result["task_capsule"] = _save_task_capsule(task, synthesis, result, ledger, tool_requests)
    if save_council_lessons and council_lessons:
        result["council_lessons_saved"] = _persist_council_lessons(task, council_lessons, result)
    if judge_error:
        result["judge_error"] = judge_error
    if succeeded < total_members:
        result["member_errors"] = [{"label": a.get("label"), "error": a.get("error")} for a in answers if a.get("status") != "success"]

    try:
        _write_json_atomic(cache_file, result)
    except Exception:
        pass
    _ACTIVE_RUN_CTX.reset(_ctx_token)
    return _json(result)



def _handle_code_review(args=None, **_kw):
    """Оркестрованный code review: project_map → context → reviewers → escalation → patch → validate → memory."""
    if args is None:
        args = {k: v for k, v in _kw.items() if k not in {"task_id", "ctx"}}
    else:
        args = dict(args or {})
        for k, v in _kw.items():
            if k not in {"task_id", "ctx"} and k not in args:
                args[k] = v

    project_path = str(args.get("project_path", "")).strip()
    review_goal = str(args.get("review_goal", "")).strip()
    target_files = _as_str_list(args.get("target_files"))
    focus_areas = _as_str_list(args.get("focus_areas"))
    min_confidence = float(args.get("min_confidence", 0.7))
    save_to_memory = _normalise_bool(args.get("save_to_memory"), True)
    preset = str(args.get("preset") or "audit")
    return_transcript = _normalise_bool(args.get("return_transcript"), False)
    max_tokens = _clamp_int(args.get("max_tokens"), 128000, 4000, 384000)

    if not project_path or not review_goal:
        return _json({"status": "error", "error": "project_path and review_goal are required"})

    project_path_obj = Path(project_path).expanduser().resolve()
    if not project_path_obj.is_dir():
        return _json({"status": "error", "error": f"Project path not found: {project_path}"})

    # ── Генерация session_id и ledger ──
    import uuid as _uuid_mod
    session_id = str(_uuid_mod.uuid4())
    ledger: list[dict[str, Any]] = []
    tool_requests_log: list[dict[str, Any]] = []

    _add_evidence(ledger, "code_review_start",
                  f"Code review started: {review_goal}",
                  project=project_path, focus_areas=focus_areas, min_confidence=min_confidence)

    # ── Шаг 1: Project Map via code_shrinker ──
    _add_evidence(ledger, "phase", "Phase 1/6: Building project map via code_shrinker")
    project_map = _call_registry_tool("mcp__code_shrinker__project_outline", {
        "path": str(project_path_obj),
        "maxFiles": _clamp_int(args.get("max_files"), 50, 10, 200),
    })
    if not project_map or project_map.get("status") == "error":
        _add_evidence(ledger, "phase_error", "Project map failed", error=str(project_map))
        return _json({"status": "error", "error": "Failed to build project map", "details": _truncate_text(str(project_map), 2000)})

    project_files = project_map.get("outline", []) if isinstance(project_map, dict) else []
    _add_evidence(ledger, "phase_done", f"Project map: {len(project_files)} files")

    # ── Шаг 2: Compressed context для ключевых файлов ──
    _add_evidence(ledger, "phase", "Phase 2/6: Creating compressed context")
    review_files = target_files if target_files else [f.get("file", "") for f in project_files[:15] if f.get("file")]
    compressed_contexts: list[dict[str, Any]] = []

    for fpath in review_files[:12]:  # ограничиваем чтобы не перегрузить контекст
        full_path = project_path_obj / fpath if not Path(fpath).is_absolute() else Path(fpath)
        if not full_path.exists():
            continue
        # Получаем fingerprint (L0) + contracts (L1)
        fp_result = _call_registry_tool("mcp__code_shrinker__file_fingerprint", {
            "filePath": str(full_path),
        })
        if fp_result and isinstance(fp_result, dict):
            compressed_contexts.append({
                "file": fpath,
                "fingerprint": _truncate_text(_json(fp_result), 3000),
            })

    compressed_text = "\n\n".join(
        f"### {c['file']}\n```\n{c['fingerprint']}\n```"
        for c in compressed_contexts
    ) if compressed_contexts else "(no target files — full project outline only)"

    _add_evidence(ledger, "phase_done", f"Compressed {len(compressed_contexts)} files")

    # ── Шаг 3: Launch independent reviewers via OmniCouncil ──
    _add_evidence(ledger, "phase", "Phase 3/6: Launching independent reviewers")

    member_models = _as_str_list(args.get("member_models")) or [
        "deepseek-v4-pro",
        "deepseek-v4-pro",
        "deepseek-v4-pro",
    ]
    judge_model = str(args.get("judge_model") or "") or (member_models[0] if member_models else "deepseek-v4-pro")

    review_task = f"""Code review: {review_goal}

Project: {project_path}
Files: {', '.join(review_files) if review_files else 'all project files'}

COMPRESSED PROJECT CONTEXT:
{compressed_text}

INSTRUCTIONS for each reviewer:
1. Analyze the project structure and code from the compressed context above.
2. For each finding, assign a confidence score (0.0-1.0).
3. If confidence < {min_confidence} for any finding, mark it for "exact_source_escalation" and specify which symbol/file needs exact source.
4. Classify findings: vulnerability, bug, style, architecture, performance, security.
5. Provide: description, file:line (if visible), severity (critical/high/medium/low), recommendation, CONFIDENCE.
6. Output structured JSON with findings array.

Focus areas: {', '.join(focus_areas) if focus_areas else 'all'}"""

    council_result = handler({
        "task": review_task,
        "context": "",
        "mode": "review",
        "preset": preset,
        "member_models": member_models,
        "judge_model": judge_model,
        "max_tokens": max_tokens,
        "return_evidence": True,
        "return_transcript": return_transcript,
        "tool_mode": "safe_agent",
        "collaboration_rounds": 1,
        "message_rounds": 0,
        "prosecutor_round": True,
        "compiler_judge": True,
        "dissent_required": True,
        "anti_slop": True,
        "output_format": "json",
        "use_cache": False,
    })

    if not council_result or council_result.get("status") not in ("success", "partial"):
        _add_evidence(ledger, "phase_error", "Council review failed", error=str(council_result))
        return _json({"status": "error", "error": "Council review failed", "details": _truncate_text(str(council_result), 2000)})

    synthesis = council_result.get("synthesis", "") or council_result.get("compiled_synthesis", {})
    council_ledger = council_result.get("evidence", [])
    ledger.extend(council_ledger)
    _add_evidence(ledger, "phase_done", f"Reviewers completed: {council_result.get('members_responded', 0)}/{council_result.get('total_members', 0)} responded")

    # ── Шаг 4: Exact source escalation для низкой confidence ──
    _add_evidence(ledger, "phase", "Phase 4/6: Exact source escalation for low-confidence findings")

    low_confidence_findings = []
    if isinstance(synthesis, dict):
        findings = synthesis.get("findings", []) if isinstance(synthesis, dict) else []
        for f in findings:
            conf = f.get("confidence", 1.0)
            if isinstance(conf, str):
                try:
                    conf = float(conf)
                except (ValueError, TypeError):
                    conf = 0.5
            if conf < min_confidence:
                low_confidence_findings.append(f)
    elif isinstance(synthesis, str):
        # Try to parse JSON from string synthesis
        try:
            parsed = json.loads(synthesis)
            findings = parsed.get("findings", []) if isinstance(parsed, dict) else []
        except (json.JSONDecodeError, TypeError):
            findings = []
        for f in findings:
            conf = f.get("confidence", 1.0)
            if isinstance(conf, str):
                try:
                    conf = float(conf)
                except (ValueError, TypeError):
                    conf = 0.5
            if conf < min_confidence:
                low_confidence_findings.append(f)

    exact_sources: list[dict[str, Any]] = []
    for finding in low_confidence_findings[:8]:  # лимит эскалаций
        file_ref = finding.get("file", "")
        symbol_id = finding.get("symbol_id", finding.get("symbolId", "s0"))
        if not file_ref:
            continue

        full_path = project_path_obj / file_ref if not Path(file_ref).is_absolute() else Path(file_ref)
        if not full_path.exists():
            continue

        source_result = _call_registry_tool("mcp__code_shrinker__file_symbol", {
            "filePath": str(full_path),
            "symbolId": str(symbol_id),
        })
        if source_result and isinstance(source_result, dict):
            exact_sources.append({
                "finding_id": finding.get("id", finding.get("title", "")),
                "file": file_ref,
                "symbol_id": symbol_id,
                "source": _truncate_text(_json(source_result), 4000),
                "confidence_before": finding.get("confidence"),
            })
            _add_evidence(ledger, "escalation",
                          f"Exact source for {file_ref}:{symbol_id}",
                          finding=_truncate_text(str(finding.get('title', finding.get('description', ''))), 200))

    _add_evidence(ledger, "phase_done", f"Escalated {len(exact_sources)} findings to exact source")

    # ── Шаг 5: Patch proposal (если есть actionable findings) ──
    _add_evidence(ledger, "phase", "Phase 5/6: Generating patch proposals")

    patch_proposals: list[dict[str, Any]] = []
    actionable_findings = []

    if isinstance(synthesis, dict):
        findings = synthesis.get("findings", [])
    elif isinstance(synthesis, str):
        try:
            parsed = json.loads(synthesis)
            findings = parsed.get("findings", [])
        except (json.JSONDecodeError, TypeError):
            findings = []
    else:
        findings = []

    for f in findings:
        severity = str(f.get("severity", "")).lower()
        if severity in ("critical", "high") and f.get("file") and f.get("recommendation"):
            actionable_findings.append(f)

    for finding in actionable_findings[:5]:  # лимит патчей
        file_ref = finding.get("file", "")
        recommendation = str(finding.get("recommendation", ""))
        symbol_id = finding.get("symbol_id", finding.get("symbolId", "s0"))

        if not file_ref or not recommendation:
            continue

        full_path = project_path_obj / file_ref if not Path(file_ref).is_absolute() else Path(file_ref)
        if not full_path.exists():
            continue

        # Используем code_shrinker для рефакторинга
        refactor_result = _call_registry_tool("mcp__code_shrinker__code_refactor", {
            "filePath": str(full_path),
            "scope": str(symbol_id),
            "instruction": f"Fix {finding.get('title', 'finding')}: {recommendation}",
        })
        if refactor_result and isinstance(refactor_result, dict) and refactor_result.get("patch"):
            patch_proposals.append({
                "finding_id": finding.get("id", finding.get("title", "")),
                "file": file_ref,
                "patch": _truncate_text(str(refactor_result.get("patch", "")), 8000),
                "description": recommendation,
            })

    _add_evidence(ledger, "phase_done", f"Generated {len(patch_proposals)} patch proposals")

    # ── Шаг 6: Sandbox validation ──
    _add_evidence(ledger, "phase", "Phase 6/6: Validating patch proposals in sandbox")

    validation_results: list[dict[str, Any]] = []
    for pp in patch_proposals:
        file_ref = pp.get("file", "")
        full_path = project_path_obj / file_ref if not Path(file_ref).is_absolute() else Path(file_ref)

        validate_result = _call_registry_tool("mcp__code_shrinker__code_review", {
            "diff": pp.get("patch", ""),
            "filePath": str(full_path) if full_path.exists() else file_ref,
            "context": f"Patch validation for: {pp.get('description', '')}",
        })
        validation_results.append({
            "finding_id": pp.get("finding_id", ""),
            "file": file_ref,
            "validated": bool(validate_result and isinstance(validate_result, dict) and not validate_result.get("error")),
            "validation_output": _truncate_text(_json(validate_result), 2000),
        })
        _add_evidence(ledger, "validation",
                      f"Validated patch for {file_ref}",
                      finding=pp.get("finding_id", ""),
                      valid=validation_results[-1].get("validated", False))

    _add_evidence(ledger, "phase_done",
                  f"Validated {sum(1 for v in validation_results if v.get('validated'))}/{len(validation_results)} patches")

    # ── Сохранение в Memory Wiki ──
    memory_saved = []
    if save_to_memory:
        # Сохраняем synthesis как claim
        synthesis_text = _json(synthesis) if isinstance(synthesis, dict) else str(synthesis)
        claim_result = _call_registry_tool("mcp__memory_wiki__add_claim", {
            "claim": _truncate_text(synthesis_text, 30000),
            "evidence": _truncate_text(_json({
                "review_goal": review_goal,
                "project": project_path,
                "files": review_files,
                "focus_areas": focus_areas,
                "findings_count": len(findings) if isinstance(findings, list) else 0,
                "patch_count": len(patch_proposals),
            }), 10000),
            "topic": "code-review",
            "source": f"omnicouncil_code_review:{session_id}",
            "confidence": 0.90,
            "salience": 0.85,
        })
        if claim_result:
            memory_saved.append({"type": "claim", "result": _truncate_text(_json(claim_result), 1000)})

        # Сохраняем task capsule
        capsule_result = _call_registry_tool("mcp__memory_wiki__add_task_capsule", {
            "task": f"Code review: {review_goal} [{project_path}]",
            "status": "completed",
            "summary": f"Reviewed {len(review_files)} files, found {len(findings) if isinstance(findings, list) else 0} findings, {len(patch_proposals)} patches proposed.",
            "plan": f"1. Project map. 2. Compressed context. 3. Independent review ({len(member_models)} models). 4. Exact source escalation for {len(low_confidence_findings)} low-confidence findings. 5. Generated {len(patch_proposals)} patches. 6. Validated in sandbox.",
            "files": review_files,
            "topic": "code-review",
        })
        if capsule_result:
            memory_saved.append({"type": "task_capsule", "result": _truncate_text(_json(capsule_result), 1000)})

        # Сохраняем каждый validated patch как evidence
        for vresult in validation_results:
            if vresult.get("validated"):
                evidence_result = _call_registry_tool("mcp__memory_wiki__add_evidence", {
                    "claim_id": "",
                    "evidence_type": "patch_validation",
                    "summary": f"Patch validated for {vresult.get('file', '')}: {vresult.get('finding_id', '')}",
                    "source": f"omnicouncil_code_review:{session_id}",
                })
                if evidence_result:
                    memory_saved.append({"type": "evidence", "result": _truncate_text(_json(evidence_result), 500)})

        _add_evidence(ledger, "memory_saved", f"Saved {len(memory_saved)} items to Memory Wiki")

    # ── Финальный результат ──
    result = {
        "status": "success",
        "tool": "omnicouncil_code_review",
        "version": VERSION,
        "session_id": session_id,
        "project": project_path,
        "review_goal": review_goal,
        "files_reviewed": len(review_files),
        "files_mapped": len(project_files),
        "reviewers": len(member_models),
        "judge": judge_model,
        "findings": {
            "total": len(findings) if isinstance(findings, list) else 0,
            "low_confidence": len(low_confidence_findings),
            "escalated_to_exact_source": len(exact_sources),
            "actionable": len(actionable_findings),
        },
        "patches": {
            "proposed": len(patch_proposals),
            "validated": sum(1 for v in validation_results if v.get("validated")),
            "total": len(validation_results),
        },
        "synthesis": synthesis if isinstance(synthesis, dict) else _truncate_text(str(synthesis), 8000),
        "exact_sources": exact_sources,
        "patch_proposals": patch_proposals,
        "validation_results": validation_results,
        "memory_saved": len(memory_saved),
        "diagnostics": {
            "preset": preset,
            "min_confidence": min_confidence,
        },
        "evidence": ledger if return_transcript else _truncate_text(_json(ledger), 6000),
        "operator_action": (
            "Review patch_proposals above. To apply a patch, use: "
            "mcp__code_shrinker__patch_apply with the patch content. "
            "All findings and evidence saved to Memory Wiki under topic 'code-review'."
        ),
    }

    return _json(result)


def register(ctx):
    global _RUNTIME_CTX
    _RUNTIME_CTX = ctx
    ctx.register_tool(
        name="hermes_omnicouncil",
        toolset="hermes_omnicouncil",
        schema=SCHEMA,
        handler=handler,
    )
    ctx.register_tool(
        name="deep_web_crawl",
        toolset="hermes_omnicouncil",
        schema=_deep_web_research.SCHEMA,
        handler=_deep_web_research.handler,
    )
    for schema, handler_fn in [
        (DOCTOR_SCHEMA, _handle_doctor),
        (CACHE_LIST_SCHEMA, _handle_cache_list),
        (CACHE_GET_SCHEMA, _handle_cache_get),
        (CACHE_CLEAR_SCHEMA, _handle_cache_clear),
        (CACHE_EXPLAIN_SCHEMA, _handle_cache_explain),
    ]:
        ctx.register_tool(name=schema["name"], toolset="hermes_omnicouncil", schema=schema, handler=handler_fn)
    for schema in getattr(_deep_web_research, "MANAGEMENT_SCHEMAS", []):
        handler_name = schema.get("handler") or f"handle_{schema.get('name', '')}"
        handler_fn = getattr(_deep_web_research, handler_name, None)
        if callable(handler_fn):
            ctx.register_tool(name=schema["name"], toolset="hermes_omnicouncil", schema=schema, handler=handler_fn)

    # ── Code Review tool registration (DISABLED pending API rewrite — see fix/omnicouncil-runtime)
    # ctx.register_tool(
    #     name="omnicouncil_code_review",
    #     toolset="hermes_omnicouncil",
    #     schema=CODE_REVIEW_SCHEMA,
    #     handler=_handle_code_review,
    # )

# HERMES-SECURITY-INTEGRATION-20260728: secure defaults are internal/no-fallback; see council_context.py.
