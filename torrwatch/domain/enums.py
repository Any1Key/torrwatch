"""Persistent state values and their permitted Phase 1 transitions."""

from __future__ import annotations

from enum import StrEnum


class InitialSyncMode(StrEnum):
    BASELINE_ONLY = "BASELINE_ONLY"
    BASELINE_AND_DELIVERY = "BASELINE_AND_DELIVERY"


class MonitorStatus(StrEnum):
    HEALTHY = "HEALTHY"
    PAUSED = "PAUSED"
    ERROR = "ERROR"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    INVALID_TARGET = "INVALID_TARGET"
    PLUGIN_PARSE_ERROR = "PLUGIN_PARSE_ERROR"
    PROXY_ERROR = "PROXY_ERROR"


class JobType(StrEnum):
    MONITOR_CHECK = "MONITOR_CHECK"


class JobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_PERMANENT = "FAILED_PERMANENT"


CLAIMABLE_JOB_STATUSES = (JobStatus.PENDING, JobStatus.FAILED_RETRYABLE)
TERMINAL_JOB_STATUSES = (JobStatus.SUCCESS, JobStatus.FAILED_PERMANENT)


class ProxyType(StrEnum):
    DIRECT = "DIRECT"
    HTTP = "HTTP"
    SOCKS5 = "SOCKS5"


class ProxyFallbackMode(StrEnum):
    DISABLED = "DISABLED"
    DIRECT = "DIRECT"
    PROFILE = "PROFILE"
