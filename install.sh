#!/usr/bin/env bash
# Installs selected Python tools from the repository's GitHub source.

# Leave out -u: the bash 3.2 that ships with macOS treats an empty array as
# unset, so expanding the empty result lists would stop the script.
set -eo pipefail

# The tools this script can install, in menu order. Each name is also the
# package folder under packages/.
TOOL_NAMES=(agent-board agent-run doc-paths friction page-to-markdown progress review-feedback team-send)

# The one-line purpose shown beside each tool, in the same order as TOOL_NAMES.
TOOL_PURPOSES=(
	'Show HCOM teams that need your attention'
	'Run project commands with bounded output'
	'Check local paths named in Markdown files'
	'Record and review agent workflow friction'
	'Convert a page or HTML file to Markdown'
	'Track local project tasks and progress'
	'Prepare review comments and patches (also installs review-patches)'
	'Send a message to one HCOM teammate'
)

# Installs the named tools, or the ones picked from a menu when no names are
# given, then prints which installed and which failed. Exits non-zero when the
# menu cannot read from the terminal, nothing is selected, a name is unknown,
# uv is missing, or any install fails.
#
# @param  {string}  ...
#     Tool names or menu numbers, --all, or --help. No arguments open the menu.
main() {
	# The tool names or menu numbers the user asked for.
	local selections=("$@")
	# The tools to install, each listed once.
	local requested=()
	# The tools that installed successfully.
	local installed=()
	# The tools that failed to install.
	local failed=()
	# The user's menu answer. It is read from the terminal because, when the
	# script is piped from curl, standard input holds the script itself.
	local menu_answer
	# The name or menu number being checked.
	local choice
	# The tool a choice refers to, and later the tool being installed.
	local tool_name

	if [[ "${1:-}" == '--help' && "$#" -eq 1 ]]; then
		cat <<'HELP'
Install Python tools from the agent-tools GitHub repository.

Usage: ./install.sh [tool-name-or-number ... | --all | --help]

With no arguments, choose one or more tools from a menu. Enter numbers or
names separated by spaces or commas. The menu also works with curl | bash.
Tool names or numbers given on the command line, and --all, install without
a prompt. Running the script again reinstalls the latest version from the
GitHub main branch.

Tools:
HELP
		print_tools
		printf '\nRemove a tool with: uv tool uninstall <tool-name>\n'
		return 0
	fi

	if [[ "$#" -eq 0 ]]; then
		printf 'Choose the tools to install:\n'
		print_tools
		printf 'Enter numbers or names, separated by spaces or commas: '

		# Without a terminal, bash's own error about /dev/tty would print
		# before the message below, so it is hidden.
		if ! { read -r menu_answer </dev/tty; } 2>/dev/null; then
			printf '\nCould not read a choice from the terminal. Pass tool names or --all instead.\n' >&2
			return 1
		fi

		menu_answer="${menu_answer//,/ }"
		read -r -a selections <<< "$menu_answer"
	elif [[ "$#" -eq 1 && "$1" == '--all' ]]; then
		selections=("${TOOL_NAMES[@]}")
	fi

	if [[ "${#selections[@]}" -eq 0 ]]; then
		printf 'No tools selected.\n' >&2
		return 1
	fi

	for choice in "${selections[@]}"; do
		tool_name="$choice"
		if [[ "$choice" =~ ^[1-9][0-9]*$ ]] && (( choice <= ${#TOOL_NAMES[@]} )); then
			tool_name="${TOOL_NAMES[$((choice - 1))]}"
		fi

		if ! contains_name "$tool_name" "${TOOL_NAMES[@]}"; then
			printf 'Unknown tool: %s\nValid tools:\n' "$choice" >&2
			print_tools >&2
			return 1
		fi

		if ! contains_name "$tool_name" "${requested[@]}"; then
			requested+=("$tool_name")
		fi
	done

	if ! command -v uv >/dev/null 2>&1; then
		printf 'uv is required to install these tools. Install uv first: https://docs.astral.sh/uv/getting-started/installation/\n' >&2
		return 1
	fi

	for tool_name in "${requested[@]}"; do
		printf 'Installing %s...\n' "$tool_name"
		if uv tool install --reinstall "git+https://github.com/lewishowles/agent-tools.git#subdirectory=packages/$tool_name"; then
			installed+=("$tool_name")
		else
			failed+=("$tool_name")
		fi
	done

	printf '\nInstalled (%d):\n' "${#installed[@]}"
	if [[ "${#installed[@]}" -gt 0 ]]; then
		printf '  %s\n' "${installed[@]}"
	fi
	printf 'Failed (%d):\n' "${#failed[@]}"
	if [[ "${#failed[@]}" -gt 0 ]]; then
		printf '  %s\n' "${failed[@]}"
		return 1
	fi
}

# Prints the numbered list of tools and their purposes used by the menu, the
# help text and the unknown-tool message.
print_tools() {
	# The position of a tool in the list, counting from zero.
	local index

	for index in "${!TOOL_NAMES[@]}"; do
		printf '  %d. %-18s %s\n' "$((index + 1))" "${TOOL_NAMES[$index]}" "${TOOL_PURPOSES[$index]}"
	done
}

# Returns 0 when the name matches one of the listed names, and 1 otherwise.
#
# @param  {string}  name
#     The name to look for.
# @param  {string}  ...
#     The names to search.
contains_name() {
	# The name to look for.
	local name="$1"
	shift
	# The listed name being compared.
	local listed_name

	for listed_name in "$@"; do
		if [[ "$listed_name" == "$name" ]]; then
			return 0
		fi
	done

	return 1
}

main "$@"
