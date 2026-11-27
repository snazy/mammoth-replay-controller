"""Mechanical log extraction: build scan URLs, HTTP 429 counts and failure classification."""

import re

COLLECTOR_VERSION = 1
LOG_TS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z ")
# Develocity prints public scans as https://gradle.com/s/<id>; older versions used scans.gradle.com.
SCAN_URL = re.compile(r"https://(?:scans\.)?gradle\.com/s/[a-z0-9]+")
HTTP_429 = re.compile(r"\b429\b.*(Too Many Requests|status code)|(Too Many Requests|status code)\D{0,5}429\b", re.IGNORECASE)
MAVEN_CENTRAL = re.compile(r"repo1?\.maven(\.apache)?\.org", re.IGNORECASE)
FAILURE_LINE = re.compile(r"FAILED|What went wrong|##\[error\]|Execution failed for task")
EXCERPT_LINES = 40

DEFAULT_INFRASTRUCTURE = [
    r"The runner has received a shutdown signal",
    r"lost communication with the server",
    r"No space left on device",
    r"The hosted runner encountered an error",
    r"Could not resolve host",
]


def strip_ts(line: str) -> str:
    return LOG_TS.sub("", line, count=1)


def infrastructure_patterns(patterns: dict) -> list[str]:
    return [*DEFAULT_INFRASTRUCTURE, *patterns.get("infrastructure", [])]


def extract(log: str, patterns: dict | None = None) -> dict:
    lines = [strip_ts(line) for line in log.splitlines()]
    scans = sorted(set(SCAN_URL.findall(log)))
    r429 = [line for line in lines if HTTP_429.search(line)]
    failures = [line.strip()[:300] for line in lines if FAILURE_LINE.search(line)]
    # infrastructure messages need not be on a failure line, e.g. Gradle's plugin resolution details
    infra = infrastructure_patterns(patterns or {})
    infra_lines = [line.strip()[:300] for line in lines if any(re.search(pat, line) for pat in infra)]
    return {
        "scan_urls": scans,
        "http_429": len(r429),
        "http_429_maven_central": sum(1 for line in r429 if MAVEN_CENTRAL.search(line)),
        "failure_excerpt": failures[:EXCERPT_LINES],
        "infrastructure_lines": infra_lines[:EXCERPT_LINES],
        "log_bytes": len(log.encode()),
    }


def classify(job_conclusion: str, extracted: dict, patterns: dict) -> str:
    """Class of a failed or cancelled job: cancelled | infrastructure | maven-central-429 | flaky-test | unknown."""
    excerpt = "\n".join(extracted.get("failure_excerpt", []))
    if job_conclusion == "cancelled" and not excerpt:
        return "cancelled"
    infra_text = "\n".join([*extracted.get("failure_excerpt", []), *extracted.get("infrastructure_lines", [])])
    for pat in infrastructure_patterns(patterns):
        if re.search(pat, infra_text):
            return "infrastructure"
    if extracted.get("http_429_maven_central", 0) > 0:
        return "maven-central-429"
    flaky = patterns.get("flaky_tests", [])
    failed_tests = [line for line in extracted.get("failure_excerpt", []) if " > " in line and "FAILED" in line]
    if flaky and failed_tests and all(any(re.search(p, t) for p in flaky) for t in failed_tests):
        return "flaky-test"
    return "unknown"
