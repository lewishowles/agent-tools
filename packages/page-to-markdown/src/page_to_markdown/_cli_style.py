#!/usr/bin/env python3
# Render caller data through the shared cli-style binary.

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence


class CliStyleError(RuntimeError):
	"""Base error for cli-style adapter failures."""


class CliStyleNotFoundError(CliStyleError):
	"""Raised when the cli-style binary cannot be found."""


class CliStyleRenderError(CliStyleError):
	"""Raised when cli-style rejects a render request."""


# Resolve the child environment while preserving explicit caller controls.
def resolve_subprocess_environment() -> dict[str, str]:
	environment = os.environ.copy()

	if (
		sys.stdout.isatty()
		and "FORCE_COLOR" not in environment
		and "NO_COLOR" not in environment
	):
		environment["FORCE_COLOR"] = "1"

	return environment


# Render caller data through `cli-style render`.
#
# @param  {str}  renderer
#     Stable renderer name accepted by `cli-style render`.
# @param  {dict}  data
#     JSON-serialisable renderer input.
def render(
	renderer: str,
	data: dict[str, Any],
	*,
	binary: str = "cli-style",
	profile: str | None = None,
	width: int | None = None,
	plain: bool = False,
	no_colour: bool = False,
	no_unicode: bool = False,
	extra_args: Sequence[str] | None = None,
) -> str:
	if not isinstance(renderer, str) or renderer == "":
		raise ValueError("renderer must be a non-empty string")

	if not isinstance(data, dict):
		raise TypeError("data must be a dict")

	resolved_binary = resolve_binary(binary)
	command = [resolved_binary, "render", renderer]

	if profile is not None:
		command.extend(["--profile", profile])

	if width is not None:
		command.extend(["--width", str(width)])

	if plain:
		command.append("--plain")

	if no_colour:
		command.append("--no-colour")

	if no_unicode:
		command.append("--no-unicode")

	if extra_args is not None:
		command.extend(extra_args)

	result = subprocess.run(
		command,
		capture_output=True,
		check=False,
		env=resolve_subprocess_environment(),
		input=json.dumps(data),
		text=True,
	)

	if result.returncode != 0:
		message = result.stderr.strip() or f"cli-style render failed with exit code {result.returncode}"
		raise CliStyleRenderError(message)

	return result.stdout.rstrip("\n")


# Render a result status line from Python values.
#
# @param  {str}  type
#     Result type such as success, failed, warning, or skipped.
# @param  {str}  label
#     Status label.
# @param  {str}  detail
#     Optional detail text.
def status(
	type: str,
	label: str = "",
	detail: str = "",
	**kwargs: Any,
) -> str:
	return render("status", {"type": type, "label": label, "detail": detail}, **kwargs)


# Render a labelled value row from Python values.
#
# @param  {str}  label
#     Row label.
# @param  {str}  value
#     Row value.
# @param  {str}  result
#     Optional result type for a symbol and tone.
# @param  {int|None}  label_width
#     Optional minimum label width.
# @param  {str|None}  label_colour
#     Optional label colour token.
# @param  {str|None}  value_colour
#     Optional value colour token.
# @param  {str|None}  separator
#     Optional text between the label and value.
def row(
	label: str,
	value: str,
	result: str = "",
	label_width: int | None = None,
	label_colour: str | None = None,
	value_colour: str | None = None,
	separator: str | None = None,
	**kwargs: Any,
) -> str:
	data = {
		"label": label,
		"value": value,
		"result": result,
		"labelWidth": label_width,
		"separator": separator,
	}

	if label_colour is not None:
		data["labelColour"] = label_colour

	if value_colour is not None:
		data["valueColour"] = value_colour

	return render("row", data, **kwargs)


# Render an inline span from Python values.
#
# @param  {str}  value
#     Span text.
# @param  {str}  tone
#     Optional colour tone, defaulting to info.
# @param  {str|None}  weight
#     Optional ANSI text weight.
def span(
	value: str,
	tone: str = "info",
	weight: str | None = None,
	**kwargs: Any,
) -> str:
	return render("span", {"value": value, "tone": tone, "weight": weight}, **kwargs)


# Render a divider from a Python value.
#
# @param  {str}  label
#     Optional divider label.
# @param  {int|None}  divider_width
#     Optional divider width.
# @param  {str|None}  divider_colour
#     Optional divider colour token.
# @param  {str|None}  label_colour
#     Optional label colour token.
def divider(
	label: str = "",
	divider_width: int | None = None,
	divider_colour: str | None = None,
	label_colour: str | None = None,
	**kwargs: Any,
) -> str:
	data = {
		"label": label,
		"dividerWidth": divider_width,
	}

	if divider_colour is not None:
		data["dividerColour"] = divider_colour

	if label_colour is not None:
		data["labelColour"] = label_colour

	return render("divider", data, **kwargs)


# Render a hint from a Python value.
#
# @param  {str}  message
#     Hint message.
def hint(
	message: str,
	**kwargs: Any,
) -> str:
	return render("hint", {"message": message}, **kwargs)


# Render a command result pattern from Python values.
#
# @param  {str}  result
#     Result type such as success, failed, warning, or skipped.
# @param  {str}  summary
#     Summary line.
# @param  {str}  command
#     Optional command text.
# @param  {int|None}  exit_code
#     Optional integer exit code.
# @param  {str}  duration
#     Optional duration text.
# @param  {str}  detail
#     Optional detail line.
def command_result(
	result: str,
	summary: str,
	command: str = "",
	exit_code: int | None = None,
	duration: str = "",
	detail: str = "",
	**kwargs: Any,
) -> str:
	return render("command-result", {
		"result": result,
		"summary": summary,
		"command": command,
		"exitCode": exit_code,
		"duration": duration,
		"details": [detail] if detail else [],
	}, **kwargs)


# Render an audit finding pattern from Python values.
#
# @param  {str}  result
#     Result type such as success, failed, warning, or skipped.
# @param  {str}  finding
#     Finding summary.
# @param  {str}  location
#     Optional source location.
# @param  {str}  recommendation
#     Optional recommendation text.
# @param  {str}  evidence
#     Optional evidence line.
# @param  {str}  reference
#     Optional reference line.
def audit_finding(
	result: str,
	finding: str,
	location: str = "",
	recommendation: str = "",
	evidence: str = "",
	reference: str = "",
	**kwargs: Any,
) -> str:
	return render("audit-finding", {
		"result": result,
		"finding": finding,
		"location": location,
		"recommendation": recommendation,
		"evidence": [evidence] if evidence else [],
		"references": [reference] if reference else [],
	}, **kwargs)


# Render a task summary pattern from Python values.
#
# @param  {str}  result
#     Result type such as success, failed, warning, or skipped.
# @param  {str}  task
#     Task label.
# @param  {str}  summary
#     Optional summary detail.
# @param  {str}  completed
#     Optional completed item.
# @param  {str}  remaining
#     Optional remaining item.
def task_summary(
	result: str,
	task: str,
	summary: str = "",
	completed: str = "",
	remaining: str = "",
	**kwargs: Any,
) -> str:
	return render("task-summary", {
		"result": result,
		"task": task,
		"summary": summary,
		"completed": [completed] if completed else [],
		"remaining": [remaining] if remaining else [],
	}, **kwargs)


# Render a confirmation result pattern from Python values.
#
# @param  {str}  state
#     Confirmation state such as confirmed, cancelled, or skipped.
# @param  {str}  action
#     Action label.
# @param  {str}  item
#     Optional item label.
# @param  {str}  detail
#     Optional detail text.
def confirmation_result(
	state: str,
	action: str,
	item: str = "",
	detail: str = "",
	**kwargs: Any,
) -> str:
	return render("confirmation-result", {
		"state": state,
		"action": action,
		"item": item,
		"detail": detail,
	}, **kwargs)


# Render a next-step block pattern from Python values.
#
# @param  {str}  next_step
#     Next step text.
# @param  {str}  reason
#     Optional reason text.
# @param  {str}  command
#     Optional command line.
# @param  {str}  alternative
#     Optional alternative line.
def next_step_block(
	next_step: str,
	reason: str = "",
	command: str = "",
	alternative: str = "",
	**kwargs: Any,
) -> str:
	return render("next-step-block", {
		"next": next_step,
		"reason": reason,
		"commands": [command] if command else [],
		"alternatives": [alternative] if alternative else [],
	}, **kwargs)


# Resolve a command name or executable path for subprocess use.
#
# @param  {str}  binary
#     Binary path or command name.
def resolve_binary(binary: str) -> str:
	if "/" in binary:
		path = Path(binary)

		if path.is_file() and os.access(path, os.X_OK):
			return str(path)

		raise CliStyleNotFoundError(f"cli-style binary not found: {binary}")

	resolved = shutil.which(binary)

	if resolved is None:
		raise CliStyleNotFoundError(f"cli-style binary not found: {binary}")

	return resolved
