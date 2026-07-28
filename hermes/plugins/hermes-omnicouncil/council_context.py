"""
CouncilRunContext — изолированный контекст одного council-запуска.
Заменяет модульные глобалы (_OMNICOUNCIL_SESSION_ID, _MODEL_PROVIDER_MAP, etc.)
на неизменяемый dataclass, передаваемый явно во все функции.

Без этого параллельные запуски смешивают session_id, provider'ы и результаты.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

# ── CouncilRunContext ────────────────────────────────────────────────
@dataclass(frozen=True)
class CouncilRunContext:
    """Изолированный контекст одного council-запуска. Неизменяемый после создания."""

    run_id: str
    session_id: str
    namespace: str  # "omnicouncil:blackboard:{run_id}"

    # Provider routing
    model_provider_map: Mapping[str, str | None] = field(default_factory=dict)
    judge_provider: str | None = None
    research_provider: str | None = None
    fallback_models: tuple[str, ...] = ()

    # Budget & deadline
    deadline: float | None = None
    budget: RunBudget | None = None

    # Provider data policy: "confidential" | "internal" | "public"
    provider_data_policy: str = "internal"  # secure default: code and memory stay on trusted providers

    # Implicit HTTP fallback OFF by default (P0 #6)
    implicit_http_fallback: bool = False  # fail closed unless a caller explicitly opts in

    # ── P1 #8: Cancellation token ──
    # threading.Event — при .set() council должен остановиться.
    # Проверяется через run_ctx.is_cancelled() перед каждым model/tool call.
    _cancellation_token: Any = field(default_factory=lambda: __import__('threading').Event())

    def is_cancelled(self) -> bool:
        """Проверить, не отменён ли запуск. Потокобезопасно."""
        return self._cancellation_token.is_set()

    def cancel(self):
        """Отменить запуск. После этого все новые model/tool calls будут блокированы."""
        self._cancellation_token.set()


# ── Context manager for safe setup/teardown ─────────────────────────
import contextlib as _contextlib_module

@_contextlib_module.contextmanager
def _run_context(ctx_var, run_ctx: CouncilRunContext):
    """P0 #1: try/finally контекстный менеджер для ContextVar.
    
    Использование:
        with _run_context(_ACTIVE_RUN_CTX, run_ctx):
            # council execution
    
    Гарантирует reset даже при исключениях и ранних return.
    """
    token = ctx_var.set(run_ctx)
    try:
        yield
    finally:
        ctx_var.reset(token)


# ── RunBudget ────────────────────────────────────────────────────────
@dataclass
class RunBudget:
    """Общий бюджет council-запуска. Контролирует токены, стоимость, время, вызовы."""

    max_total_tokens: int = 1_000_000
    max_total_cost_usd: float = 5.0
    max_wall_time_seconds: float = 600.0
    max_model_calls: int = 50
    max_tool_calls: int = 200
    max_failed_calls: int = 10
    reserved_judge_tokens: int = 50_000

    # P1 fix: threading.Lock для атомарных операций бюджета
    _lock: Any = field(default_factory=lambda: __import__('threading').Lock(), repr=False)

    # Расходуемые счётчики
    spent_input_tokens: int = 0
    spent_output_tokens: int = 0
    spent_cost_usd: float = 0.0
    model_calls: int = 0
    tool_calls: int = 0
    failed_calls: int = 0
    started_at: float = field(default_factory=time.time)

    @property
    def spent_total_tokens(self) -> int:
        return self.spent_input_tokens + self.spent_output_tokens

    @property
    def remaining_tokens(self) -> int:
        return max(0, self.max_total_tokens - self.spent_total_tokens)

    @property
    def remaining_cost(self) -> float:
        return max(0.0, self.max_total_cost_usd - self.spent_cost_usd)

    @property
    def elapsed_seconds(self) -> float:
        return time.time() - self.started_at

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.max_wall_time_seconds - self.elapsed_seconds)

    def try_reserve_model_call(self, estimated_tokens: int = 0, estimated_cost: float = 0.0) -> bool:
        """P1 fix: атомарная reservation. Возвращает True если вызов разрешён."""
        with self._lock:
            if self.model_calls >= self.max_model_calls:
                return False
            if self.spent_total_tokens + estimated_tokens + self.reserved_judge_tokens > self.max_total_tokens:
                return False
            if self.spent_cost_usd + estimated_cost > self.max_total_cost_usd:
                return False
            if self.elapsed_seconds > self.max_wall_time_seconds:
                return False
            if self.failed_calls >= self.max_failed_calls:
                return False
            # Reservation: сразу резервируем токены и вызов
            self.model_calls += 1
            self.spent_input_tokens += estimated_tokens
            self.spent_cost_usd += estimated_cost
            return True

    def can_call_model(self, estimated_tokens: int = 0) -> bool:
        """Можно ли сделать ещё один model call с учётом резерва judge."""
        if self.model_calls >= self.max_model_calls:
            return False
        if self.spent_total_tokens + estimated_tokens + self.reserved_judge_tokens > self.max_total_tokens:
            return False
        if self.elapsed_seconds > self.max_wall_time_seconds:
            return False
        if self.failed_calls >= self.max_failed_calls:
            return False
        if self.spent_cost_usd >= self.max_total_cost_usd:
            return False
        return True

    def can_call_tool(self) -> bool:
        return self.tool_calls < self.max_tool_calls

    def record_model_call(self, input_tokens: int, output_tokens: int, cost_usd: float = 0.0):
        """Record actual token/cost usage. Does NOT increment model_calls — that was done by try_reserve_model_call."""
        self.spent_input_tokens += input_tokens
        self.spent_output_tokens += output_tokens
        self.spent_cost_usd += cost_usd
        # model_calls already incremented by try_reserve_model_call(); don't double-count

    def record_tool_call(self):
        self.tool_calls += 1

    def record_failure(self):
        self.failed_calls += 1


# ── Provider data policies ───────────────────────────────────────────
PROVIDER_DATA_POLICIES: dict[str, dict[str, Any]] = {
    "confidential": {
        "allowed_providers": ["local"],
        "allow_implicit_fallback": False,
        "description": "Приватные данные: память пользователя, секреты, внутренние решения council",
    },
    "internal": {
        "allowed_providers": ["local", "private-openrouter-proxy"],
        "allow_implicit_fallback": False,
        "description": "Внутренние данные: код, конфигурация, планы",
    },
    "public": {
        "allowed_providers": ["local", "openrouter"],
        "allow_implicit_fallback": True,
        "description": "Публичные данные: веб-поиск, внешние API",
    },
}

# ═══════════════════════════════════════════════════════════════════
#  Prompt Injection Guard — RecalledItem + quarantine pipeline
# ═══════════════════════════════════════════════════════════════════

@dataclass
class RecalledItem:
    """Элемент памяти, прошедший security pipeline. Всегда данные, не инструкция."""

    content: str
    source: str
    memory_type: str
    trust_level: Literal["trusted", "verified", "untrusted", "quarantined"]
    provenance: list[str] = field(default_factory=list)
    injection_signals: list[str] = field(default_factory=list)

    @property
    def is_safe(self) -> bool:
        return self.trust_level in ("trusted", "verified") and not self.injection_signals


# ── Injection signal detection ──────────────────────────────────────
import re

_INJECTION_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Direct instruction override
    (re.compile(r"(?i)ignore\s+(all\s+)?(previous|prior|above|before)\s+(instructions?|directives?|prompts?|context)", re.I), "ignore_previous_instructions"),
    (re.compile(r"(?i)you\s+(are|now)\s+(no\s+longer|not)\s+(an?\s+)?(ai|assistant|hermes)", re.I), "role_override"),
    (re.compile(r"(?i)(override|bypass|disable)\s+(all\s+)?(safety\s+)?(filters?|constraints?|restrictions?|guidelines?)", re.I), "filter_bypass"),
    (re.compile(r"(?i)(system\s+prompt|system_prompt|system message)\s*(:|is|now|changed|updated|replaced)", re.I), "system_prompt_manipulation"),
    # Tool/role expansion
    (re.compile(r"(?i)(you\s+(can|may|should|must|will)\s+(now\s+)?)?(execute|run|perform)\s+(any|all)\s+(commands?|code|tools?)", re.I), "tool_expansion"),
    (re.compile(r"(?i)expand(ed)?\s+(your\s+)?(tool|capabilit|permission|access)", re.I), "capability_expansion"),
    (re.compile(r"(?i)grant\s+(yourself|you)\s+(admin|root|full|unrestricted|elevated)", re.I), "privilege_escalation"),
    # Policy disable
    (re.compile(r"(?i)(remove|delete|drop|clear)\s+(all\s+)?(your\s+)?(constraints?|limitations?|boundaries?|rules?|policies?)", re.I), "policy_removal"),
    (re.compile(r"(?i)(unrestricted|uncensored|unfiltered|godmode|nuclear)\s+(mode|agent|ai|model)", re.I), "unrestricted_mode_request"),
    # Token/source forgery
    (re.compile(r"(?i)source\s*:\s*(operator|admin|root|system|hermes)", re.I), "source_forgery"),
    (re.compile(r"(?i)trust_level\s*:\s*(trusted|verified)", re.I), "trust_level_forgery"),
    # Obfuscation indicators
    (re.compile(r"[​‌‍‎‏⁠⁡⁢⁣⁤﻿]"), "zero_width_chars"),
    (re.compile(r"(?i)(?:from|import)\s+(?:base64|codecs|binascii).*?(?:b64decode|a85decode|decode)", re.I), "encoding_import"),
]

def detect_injection_signals(text: str) -> list[str]:
    """Обнаружить injection-сигналы в тексте. Возвращает список найденных паттернов."""
    if not text or not isinstance(text, str):
        return []
    signals: list[str] = []
    for pattern, signal_name in _INJECTION_PATTERNS:
        if pattern.search(text):
            signals.append(signal_name)
    # Base64 encoding check (heuristic)
    if re.search(r'(?i)(?:ZnJvbXxpbXBvcnQ=|aWdub3Jl|ZGlzYWJsZQ==)', text):
        signals.append("base64_encoded_directive")
    return signals


def sanitize_recalled(text: str | None, source: str, memory_type: str) -> RecalledItem:
    """Fail-closed sanitization. Ошибка → quarantine, НЕ original_text."""
    try:
        if text is None or not isinstance(text, (str, bytes)):
            return RecalledItem(
                content="", source=source, memory_type=memory_type,
                trust_level="quarantined", provenance=[],
                injection_signals=["sanitization_failed:invalid_input_type"],
            )
        content = str(text)[:10000]
        if not content.strip():
            return RecalledItem(
                content="", source=source, memory_type=memory_type,
                trust_level="quarantined", provenance=[],
                injection_signals=["sanitization_failed:empty_content"],
            )
        signals = detect_injection_signals(content)
        trust: Literal["trusted", "verified", "untrusted", "quarantined"] = "untrusted"
        if signals:
            trust = "quarantined"
        elif source.startswith("post_task:smoke") and memory_type == "claim":
            trust = "verified"
        elif source.startswith("curated:") or source.startswith("memory_maintenance:"):
            trust = "trusted"
        return RecalledItem(
            content=content,
            source=source,
            memory_type=memory_type,
            trust_level=trust,
            provenance=[source],
            injection_signals=signals,
        )
    except Exception as exc:
        return RecalledItem(
            content="",
            source=source,
            memory_type=memory_type,
            trust_level="quarantined",
            provenance=[],
            injection_signals=[f"sanitization_failed:{type(exc).__name__}"],
        )


# HERMES-SECURITY-INTEGRATION-20260728: authoritative shared trust implementation.
try:
    import os as _trust_os, sys as _trust_sys
    from pathlib import Path as _TrustPath
    _trust_lib = _TrustPath(_trust_os.environ.get("HERMES_HOME", str(_TrustPath.home() / ".hermes"))).expanduser() / "lib"
    if str(_trust_lib) not in _trust_sys.path:
        _trust_sys.path.insert(0, str(_trust_lib))
    from hermes_trust_core import (
        RecalledItem as RecalledItem,
        detect_injection_signals as detect_injection_signals,
        sanitize_recalled as sanitize_recalled,
    )
except Exception as _trust_exc:
    if _trust_os.environ.get("HERMES_SECURITY_STRICT", "1").lower() not in {"0", "false", "no", "off"}:
        raise RuntimeError(f"shared hermes_trust_core unavailable: {_trust_exc}") from _trust_exc
