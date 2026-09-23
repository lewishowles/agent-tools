"""Read compiler and test failures from xcodebuild output."""

import re
from collections.abc import Sequence
from pathlib import Path

from agent_run.failures import Failure, FailureReport

# A source error, such as "File.swift:12:9: error: message". The column,
# or both the line and the column, may be missing. Tools such as clang and ld
# print "clang: error: message" in the same shape, with their name as the prefix.
_SOURCE_ERROR = re.compile(
    r"^(?P<path>.+?)(?::(?P<line>\d+)(?::(?P<column>\d+))?)?: error: (?P<title>.+)$"
)
# An error with no source file, such as a code-signing or build-setting problem.
_BARE_ERROR = re.compile(r"^error: (?P<title>.+)$")
# A failed XCTest assertion, which names the test as "-[Suite testName]".
_XCTEST_ERROR = re.compile(r"^(?P<path>.+):(?P<line>\d+): error: (?P<title>-\[.+)$")
# The XCTest line that marks a whole test case as failed.
_TEST_CASE = re.compile(r"^Test Case '(.+)' failed(?: .*)?$")
# A failed Swift Testing expectation. Xcode starts the line with a private-use
# SF Symbols character, so the pattern matches the text after it.
_SWIFT_TEST = re.compile(
    r"(?P<title>Test .+ recorded an issue at (?P<path>.+):(?P<line>\d+):(?P<column>\d+): .+)"
)
# Xcode's own log messages about its install, such as plug-in and simulator
# warnings. They often contain "error" but never describe the user's code.
_TIMESTAMPED_NOISE = re.compile(
    r"^\s*\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+ xcodebuild\[\d+:\d+\]"
)
# The indented lines that continue one of those install messages.
_NOISE_CONTINUATION = re.compile(
    r"^\s*(?:Referenced from:|Expected in:|Details:|Object:|Method:|Thread:|Please file a bug|Domain:|Code:|Failure Reason:|Recovery Suggestion:|CoreSimulator is out of date)"
)
# The result line that starts Xcode's list of failed build steps.
_FAILED_BLOCK = re.compile(r"^\*\* (?:BUILD|TEST) FAILED \*\*$")
# The line under "Undefined symbols for architecture ..." that names a missing
# symbol. The indented lines after it list the code that uses the symbol.
_UNDEFINED_SYMBOL = re.compile(r'^\s+"(?P<symbol>_[^\"]+)", referenced from:$')


class XcodebuildReader:
    """Read the first real error from an xcodebuild log.

    Compiler and linker errors come first, then failed tests. When neither is
    printed, the reasons Xcode lists at the end of the run are used instead.
    """

    def matches(self, argv: Sequence[str]) -> bool:
        """Recognise direct xcodebuild and xcrun xcodebuild commands."""
        if not argv:
            return False

        executable = Path(argv[0]).name
        return executable == "xcodebuild" or (
            executable == "xcrun"
            and len(argv) > 1
            and Path(argv[1]).name == "xcodebuild"
        )

    def detail_anchors(self, detail: Sequence[str]) -> tuple[int | None, int | None]:
        """Anchor nothing, so a long list of callers for a missing symbol is cut from the start."""
        return None, None

    def read(self, log_text: str) -> FailureReport | None:
        """Return the errors in the log, each listed once, or None when there are none."""
        lines = log_text.splitlines()
        diagnostics: list[Failure] = []
        tests: list[Failure] = []
        seen: set[tuple[str | None, int | None, int | None, str]] = set()

        for line in lines:
            if _TIMESTAMPED_NOISE.match(line) or _NOISE_CONTINUATION.match(line):
                continue

            swift_test = _SWIFT_TEST.search(line)
            xctest = _XCTEST_ERROR.match(line)
            source = _SOURCE_ERROR.match(line)
            bare = _BARE_ERROR.match(line)
            case = _TEST_CASE.match(line)

            if swift_test:
                failure = Failure(
                    swift_test.group("path"),
                    int(swift_test.group("line")),
                    int(swift_test.group("column")),
                    swift_test.group("title"),
                    (),
                )
                target = tests
            elif xctest:
                failure = Failure(
                    xctest.group("path"),
                    int(xctest.group("line")),
                    None,
                    xctest.group("title"),
                    (),
                )
                target = tests
            elif source:
                # Without a line number, the prefix is only a file when its last
                # part looks like one. Otherwise it names the tool that failed,
                # as in "clang: error:" or "LewTimer: ld: error:".
                prefix = source.group("path")
                line_number = source.group("line")
                last_part = prefix.rsplit(": ", 1)[-1]
                looks_like_file = "/" in last_part or bool(Path(last_part).suffix)
                path = prefix if line_number or looks_like_file else None

                failure = Failure(
                    path,
                    int(line_number) if line_number else None,
                    int(source.group("column")) if source.group("column") else None,
                    source.group("title"),
                    (),
                )
                target = diagnostics
            elif bare:
                failure = Failure(None, None, None, bare.group("title"), ())
                target = diagnostics
            elif case:
                # An XCTest assertion already names this test, so its summary adds nothing.
                if any(case.group(1) in failure.title for failure in tests):
                    continue

                failure = Failure(None, None, None, line, ())
                target = tests
            else:
                continue

            key = (failure.path, failure.line, failure.column, failure.title)
            if key not in seen:
                seen.add(key)
                target.append(failure)

        linker_failure = _undefined_symbol_failure(lines)

        failures = (
            ([linker_failure, *diagnostics] if linker_failure else diagnostics)
            or tests
            or _summary_failures(lines)
        )
        if not failures:
            return None

        return FailureReport(True, failures[0], tuple(failures[1:]), 0, False, ())


def _summary_failures(lines: Sequence[str]) -> list[Failure]:
    """Return the reasons Xcode lists under "Testing failed:" or after "** BUILD FAILED **"."""
    start = next((i for i, line in enumerate(lines) if line == "Testing failed:"), None)
    if start is not None:
        reasons = []
        for line in lines[start + 1 :]:
            if not line.strip():
                break
            if line[:1].isspace():
                reasons.append(Failure(None, None, None, line.strip(), ()))
        if reasons:
            return reasons

    start = next((i for i, line in enumerate(lines) if _FAILED_BLOCK.match(line)), None)
    if start is None:
        return []

    return [
        Failure(None, None, None, line.strip(), ())
        for line in lines[start + 1 :]
        if line.startswith(("\t", "  ")) and line.strip()
    ]


def _undefined_symbol_failure(lines: Sequence[str]) -> Failure | None:
    """Return the first missing symbol that ld reports, with the code that uses it.

    ld names the symbol and its callers before clang's general "linker command
    failed" error, so this failure is reported ahead of that error.
    """
    for index, line in enumerate(lines[:-1]):
        if not line.startswith("Undefined symbols for architecture "):
            continue

        symbol = _UNDEFINED_SYMBOL.match(lines[index + 1])
        if symbol is None:
            continue

        references = []

        for reference in lines[index + 2 :]:
            if not reference.startswith((" ", "\t")):
                break

            references.append(reference.strip())

        title = f"Undefined symbol: {symbol.group('symbol')}"
        return Failure(None, None, None, title, tuple(references))

    return None
