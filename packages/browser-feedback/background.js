// The extension APIs. Firefox names them `browser` and Chrome names them `chrome`.
const extension = globalThis.browser ?? globalThis.chrome;
// The message that tells the page's content script to show or hide the panel. It is also the shortcut's command name.
const toggleMessage = "toggle-feedback";

extension.action.onClicked.addListener(onActionClick);
extension.commands.onCommand.addListener(onCommand);

/**
 * Toggle feedback for the tab whose toolbar action was clicked.
 *
 * @param  {object}  tab
 *     The tab supplied by the browser action.
 */
function onActionClick(tab) {
	if (tab.id !== undefined) {
		void toggleFeedback(tab.id);
	}
}

/**
 * Toggle feedback in the active tab for the registered shortcut.
 *
 * @param  {string}  command
 *     The command received from the browser.
 */
function onCommand(command) {
	if (command !== toggleMessage) {
		return;
	}

	void toggleActiveTab();
}

/**
 * Find the active tab and ask its content script to toggle feedback.
 *
 * @returns {Promise<void>}
 *     Resolves after the current tab is checked.
 */
async function toggleActiveTab() {
	const [tab] = await extension.tabs.query({ active: true, currentWindow: true });

	if (tab?.id !== undefined) {
		await toggleFeedback(tab.id);
	}
}

/**
 * Ask a tab's page to show or hide its feedback panel.
 *
 * @param  {number}  tabId
 *     The tab that received the toolbar or keyboard action.
 * @returns {Promise<void>}
 *     Resolves when the page handles the message or the page cannot receive it.
 */
async function toggleFeedback(tabId) {
	try {
		await extension.tabs.sendMessage(tabId, { type: toggleMessage });
	} catch {
		// Browser-owned and restricted pages do not run content scripts.
	}
}
