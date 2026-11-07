"""Run the checks named in the settings file and summarise agent-run's results for the reviewer."""

import json
import shlex
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class CheckError(ValueError):
	"""Agent-run could not provide a usable result for the draft."""


class CheckState(StrEnum):
	"""The outcome of one check, as the reviewer sees it."""

	PASSED = "passed"
	FAILED = "failed"
	TIMED_OUT = "timed out"
	NOT_REGISTERED = "not registered"
	MANUAL = "manual"


@dataclass(frozen=True)
class CheckResult:
	"""One configured check and the result agent-run reported for it."""

	# The check name from the settings file.
	name: str
	# The command that ran, or the command a person can run by hand.
	command: str
	# The outcome shown to the reviewer.
	state: CheckState
	# How long the command ran, or None when it did not run.
	duration_seconds: float | None
	# The first failure agent-run found, or the last lines of output it kept.
	failure: str | None


# The states where the command ran and did not pass, so failure output helps.
FAILED_STATES = {CheckState.FAILED, CheckState.TIMED_OUT}

# Agent-run error codes for checks that did not run, and the state each one shows.
NOT_RUN_STATES = {
	"manual": CheckState.MANUAL,
	"not-found": CheckState.NOT_REGISTERED,
}


def run_checks(root: Path, names: tuple[str, ...]) -> tuple[CheckResult, ...]:
	"""Run each named check in order, carrying on after a check fails.

	Args:
		root: The repository whose agent-run commands are registered.
		names: The check names from the settings file.

	Returns:
		One result for each name, in settings order.

	Raises:
		CheckError: Agent-run could not be started or returned an unexpected result.
	"""
	# One result per name. Each name gets its own agent-run call, so an unknown or manual name cannot stop later checks.
	results = []

	for name in names:
		# The JSON result agent-run returned for this check.
		payload = _read_agent_run(root, name)
		results.append(_result(name, payload))

	return tuple(results)


def _read_agent_run(root: Path, name: str) -> dict:
	"""Run one registered check through agent-run and read its JSON result.

	Args:
		root: The repository where the check is registered.
		name: The registered check name.

	Returns:
		The parsed JSON result.

	Raises:
		CheckError: Agent-run could not be started or did not print valid JSON.
	"""
	try:
		# Agent-run applies the check's own timeout, so none is set here.
		process = subprocess.run(
			["agent-run", "run", name, "--json"],
			cwd=root,
			capture_output=True,
			text=True,
			check=False,
		)
		# Agent-run prints one JSON result whether the check passes or fails.
		payload = json.loads(process.stdout)
	except (OSError, ValueError) as error:
		raise CheckError(f"Could not read agent-run result for {name}.") from error

	if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
		raise CheckError(f"Invalid agent-run result for {name}.")

	return payload


def _result(name: str, payload: dict) -> CheckResult:
	"""Turn one agent-run result into the summary the reviewer reads.

	Args:
		name: The check name from the settings file.
		payload: The parsed agent-run result.

	Returns:
		The command, state, run time and first failure.

	Raises:
		CheckError: The result has an unknown error code or no run details.
	"""
	# Agent-run puts run details under data on success and under error.data on failure.
	data = payload.get("data") if payload["ok"] else _error_data(name, payload)

	if not isinstance(data, dict):
		raise CheckError(f"Invalid agent-run data for {name}.")

	# The outcome shown to the reviewer.
	state = CheckState.PASSED if payload["ok"] else _failed_state(name, payload, data)
	# Failure output, kept only for a check that ran and did not pass.
	failure = _first_failure(data) if state in FAILED_STATES else None

	return CheckResult(
		name, _command(name, data), state, data.get("duration_seconds"), failure
	)


def _error_data(name: str, payload: dict) -> object:
	"""Return the run details from a failed agent-run result.

	Args:
		name: The check name, for the error message.
		payload: The parsed agent-run result, with ok set to false.

	Returns:
		The run details, or an empty dictionary when agent-run gave none.

	Raises:
		CheckError: The result has no error object.
	"""
	# The failure description agent-run returned.
	error = payload.get("error")

	if not isinstance(error, dict):
		raise CheckError(f"Invalid agent-run error for {name}.")

	return error.get("data") or {}


def _failed_state(name: str, payload: dict, data: dict) -> CheckState:
	"""Return the reviewer's state for a check that did not pass.

	Args:
		name: The check name, for the error message.
		payload: The parsed agent-run result, with ok set to false.
		data: The run details from the result.

	Returns:
		Failed or timed out for a check that ran, otherwise manual or not registered.

	Raises:
		CheckError: Agent-run could not run the check for another reason.
	"""
	# The error object, already checked by _error_data.
	error = payload["error"]
	# The agent-run error code that says why the check did not pass.
	code = error.get("code")

	if code == "check-failed":
		return CheckState.TIMED_OUT if data.get("timed_out") else CheckState.FAILED

	if code in NOT_RUN_STATES:
		return NOT_RUN_STATES[code]

	raise CheckError(f"Agent-run could not run {name}: {error.get('message', code)}")


def _command(name: str, data: dict) -> str:
	"""Return the command to show for a check.

	Args:
		name: The check name from the settings file.
		data: The run details from the agent-run result.

	Returns:
		The registered command, or the agent-run call for a name that is not
		registered and so has no command.
	"""
	# The registered command's arguments, when agent-run found the name.
	argv = data.get("argv")

	if isinstance(argv, list) and all(isinstance(part, str) for part in argv):
		return shlex.join(argv)

	return f"agent-run run {shlex.quote(name)}"


def _first_failure(data: dict) -> str | None:
	"""Return the first failure agent-run found, or the last lines it kept.

	Args:
		data: The run details from a check that did not pass.

	Returns:
		The failure's location, title and detail, the kept output lines,
		or None when agent-run gave neither.
	"""
	# Agent-run's failure summary: a recognised first failure, or the last lines of output.
	report = data.get("failure") or {}
	# The first failure agent-run recognised in the output.
	first = report.get("first") or {}

	if first:
		# The file and line of the failure, when agent-run found them.
		location = first.get("path") or ""

		if location and first.get("line") is not None:
			location = f"{location}:{first['line']}"

		# The location, title and detail lines, skipping any that are empty.
		parts = [location, first.get("title", ""), *(first.get("detail") or [])]
		return "\n".join(part for part in parts if part) or None

	# The kept output lines, used when no failure was recognised, such as after a timeout.
	tail = report.get("tail") or []
	return "\n".join(tail) if tail else None
