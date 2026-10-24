"""Closed cause and category ids from FR-5.

Each cause pairs with exactly one category. Traffic spikes are not a cause.
"""

from enum import StrEnum


class Category(StrEnum):
    CPU = "CPU"
    MEMORY = "MEMORY"
    DISK = "DISK"
    DATABASE = "DATABASE"
    NETWORK = "NETWORK"
    DEPENDENCY = "DEPENDENCY"
    DEPLOY = "DEPLOY"
    CONFIG = "CONFIG"


class Cause(StrEnum):
    CPU_SATURATION = "cpu_saturation"
    MEMORY_LEAK = "memory_leak"
    DISK_FULL = "disk_full"
    DB_POOL_EXHAUSTED = "db_pool_exhausted"
    DNS_FAILURE = "dns_failure"
    BAD_DEPLOY = "bad_deploy"
    DEPENDENCY_TIMEOUT = "dependency_timeout"
    CONFIG_ERROR = "config_error"


class Verdict(StrEnum):
    FAULT = "FAULT"
    NOT_A_FAULT = "NOT_A_FAULT"
    UNKNOWN = "UNKNOWN"


CAUSE_CATEGORY: dict[Cause, Category] = {
    Cause.CPU_SATURATION: Category.CPU,
    Cause.MEMORY_LEAK: Category.MEMORY,
    Cause.DISK_FULL: Category.DISK,
    Cause.DB_POOL_EXHAUSTED: Category.DATABASE,
    Cause.DNS_FAILURE: Category.NETWORK,
    Cause.BAD_DEPLOY: Category.DEPLOY,
    Cause.DEPENDENCY_TIMEOUT: Category.DEPENDENCY,
    Cause.CONFIG_ERROR: Category.CONFIG,
}


CAUSE_SUMMARY: dict[Cause, str] = {
    Cause.CPU_SATURATION: "CPU stayed above the saturation threshold.",
    Cause.MEMORY_LEAK: "Memory climbed and stayed high.",
    Cause.DISK_FULL: "Disk usage crossed the fullness threshold.",
    Cause.DB_POOL_EXHAUSTED: "Database connections were exhausted.",
    Cause.DNS_FAILURE: "Name resolution failed.",
    Cause.BAD_DEPLOY: "Error rate rose after a deploy in the window.",
    Cause.DEPENDENCY_TIMEOUT: "A dependency was unhealthy while latency or errors were high.",
    Cause.CONFIG_ERROR: "A config change landed with errors in the window.",
}
